# 配置参考

[English version](CONFIG_REFERENCE.md) · [使用指南](USAGE_zh.md) · [CLI 参考](CLI_REFERENCE_zh.md)

| 配置 | 用途 |
| --- | --- |
| 模型目录（`models.toml`） | 命名连接、路由和传输设置；密钥保存在环境变量或私有 `.env`。 |
| 原生任务 TOML（`start --task-config`） | 原生任务输入和偏好，包括[可信本地扩展](#可信本地扩展)。 |
| 研究 TOML（`research-session --config`） | 结构化目标、来源、执行、写作和分析。 |
| CodeTask TOML | 可编辑项目、工作区、解释器、编辑范围和执行预算。 |

### 快速示例

保存为 `task.toml`，将 `notes.md` 放在同一目录，然后运行
`simple-ar start --task-config task.toml`：

```toml
[task]
request = "比较提供的证据并解释其局限。"
model = "env"
source = ["notes.md"]
```

来源以只读方式挂载，路径相对 TOML 文件解析。请先配置模型，见[命名模型连接](#命名模型连接)。
若需明确的结构化协议，见[研究 TOML 示例](#研究-toml分区与默认值)。

| 查阅内容 | 章节 |
| --- | --- |
| 模型 profile、选择与路由 | [命名模型连接](#命名模型连接) |
| 密钥与部署设置 | [凭据与环境变量](#凭据与环境变量) |
| 任务、研究和执行字段 | [研究 TOML](#研究-toml分区与默认值) |
| 报告和数据设置 | [写作](#已有材料写作) · [已有数据](#已有数据的描述分析) |
| 续接与追加额度 | [续接](#研究会话补齐条件与续接) |
| 项目字段、工作区和编辑范围 | [CodeTask](#codetask-toml-字段参考) · [编辑范围](#编辑范围规则) |
| 扩展描述文件与 Python 集成 | [可信本地扩展](#可信本地扩展) · [Python API](#python-创建任务) |

## 引导设置

普通 `start` 和 `--guided` 保存原生会话。结构化协议和 `--chat` 保存研究 TOML，必要时引用 CodeTask TOML；聊天设置草稿不是执行会话。续接沿用保存的输入和额度。成果偏好用于指导交付，不授予访问权限，也不禁止目标中要求的报告。

原生与结构化 TOML 的优先级均为默认值 → 文件 → 明确 CLI，CLI 列表替换文件列表。原生任务文件只包含一个 `[task]` 表。研究路径相对 TOML 目录解析，命令 argv 保持字面含义，保护路径相对执行 cwd；[CodeTask 加载](#加载规则)使用独立路径规则。

### 原生任务 TOML 字段

| `[task]` 字段 | 类型／默认值／用途 |
| --- | --- |
| `request` | 字符串目标与交付要求；除非从 CLI 提供，否则必填。 |
| `model` | 字符串选择器，默认 `env`。 |
| `source` | 只读文件或目录路径列表，相对本 TOML。 |
| `kind` | 可选成果偏好：`survey`、`bug_fix`、`reproduction`、`writing`、`data_analysis`、`figure` 或 `code`。 |
| `project`、`document`、`material` | 项目路径、原始来源路径及笔记／草稿／结果路径；后两项为列表。 |
| `data_file`、`data_path` | 数据文件路径与额外输入路径列表。 |
| `output_root`、`project_python` | 输出根目录（默认 `runs/assistant`）与可选 Python 入口路径。 |
| `allow_execution`、`allow_network`、`allow_vision` | 布尔值，默认 `false`；分别允许进程提案、公开来源获取和图像检查，每条实际进程命令还需批准。 |
| `max_turns`、`max_tokens` | 整数模型额度，默认 `30` 次请求和 `600000` token。 |
| `max_processes`、`max_process_seconds` | 累计进程调用数和墙钟时间上限，默认分别为 `max_turns × 10` 和 `max_turns × 300`；零禁用对应容量。 |
| `max_images` | 付费图像生成／编辑提交次数，整数，默认 `0`；与视觉检查分开。 |
| `from_session`、`reuse` | 已保存任务路径与新任务复用的成果组列表，见下文。 |
| `extensions` | 明确选择的路径列表，见[可信本地扩展](#可信本地扩展)。 |

以上相对路径以任务文件所在目录为基准。明确 CLI 值覆盖文件。`simple-ar start --write-task-config task.toml` 导出可编辑设置，不创建任务。

<a id="v210-任务声明开发分支"></a>

基于已有成果新建任务时，原生 `[task]` 可设置 `from_session = "../saved-task"` 和 `reuse = ["artifacts"]`，也可选 `simple-ar results` 列出的成果组。路径相对任务配置；复用材料经过字节核验，不继承权限、预算或执行状态。继续同一任务使用 `--resume-task`。

## 命名模型连接

一次读取一份模型 TOML，不合并多份文件。普通选择使用明确路径、`SIMPLE_AR_MODELS_CONFIG`，再查找 `~/.config/simple-ar/models.toml`。新原生任务还为已授权的消费者记录实际所选文件的绝对位置；续接时明确参数/当前环境优先，其次是仍存在的原定位，再查用户目录。不扫描项目目录发现配置。`models --config PATH` 只检查该文件，不会替后续命令启用它。

`start`、`models` 和客户端读取命令当前工作目录的 `.env`，不覆盖已有环境变量。模型 TOML 可以全局共用，但 `.env` 不会自动从这个全局配置目录读取。

~~~toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://provider.example/v1"
model = "provider-model-name"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]
max_output_tokens = 8000

[routes]
default = "daily"
text = "daily"
code = "daily"
writing = "daily"
~~~

替换为实际支持的端点与模型，私下设置引用的密钥变量。见[模型目录示例](../examples/model-profiles/models.toml)。`simple-ar models` 只检查配置和凭据是否存在，不调用 API。交互式 `start` 提供文本连接选择，保留明确的模型参数。结构化 `--prepare-only` 可在无密钥时保存 TOML；原生准备仍预检凭据，但不发请求。

使用官方 DeepSeek 时，可选[这份目录](../examples/model-profiles/deepseek.toml)，由 Flash 承担文本／代码和图像检查，分别配置思考强度与输出上限。官方说明了[Agent 工作的 high 档](https://api-docs.deepseek.com/guides/thinking_mode/)及[Flash 图像输入](https://api-docs.deepseek.com/guides/vision/)；这是接口能力，不是成果质量保证。新建任务前选择目录，恢复历史任务时使用其原命名连接；修改默认配置不等于授权切换旧任务。

| 选择器／使用方 | 解析规则 |
| --- | --- |
| `env` | 目录 text，再 default；无目录则用旧环境连接 |
| `profile:NAME` | 完整命名连接 |
| `route:NAME` | 用途路由，再使用能力匹配的 default |
| 嵌套 CodeTask | code，再 default，除非明确选择 |
| 原生写作 | 明确 writing 路由，否则保持任务所选连接，包括明确 profile |
| 视觉／图像 | 已配置且能力匹配的 vision/image 连接，不用文本客户端替代 |

`[model].name` 也接受选择器。有目录时，裸模型 ID 只有与按用途解析出的连接模型完全一致才被接受；不会按模型 ID 搜索 profile 或切换连接。选择其他连接请用 `profile:NAME`。名称、密钥或能力缺失时失败，不切换提供方。profile 名称须非空且不含冒号。路由为 default、text、code、writing、vision、image。

| Profile 字段 | 默认值／取值 |
| --- | --- |
| `api` | 必填：openai_chat、openai_responses、openai_images、cctq_images_async |
| `base_url`、`model`、`api_key_env`、`capabilities` | 必填：无用户信息/query/fragment 的 HTTP(S) URL；非空模型；密钥变量名；text/code/vision/image_generate/image_edit 中的非空能力列表 |
| `proxy_env` | 默认空，引用保存 HTTP(S) 代理 URL 的变量；不接受用户信息/query/fragment，实际地址不持久化 |
| `stream`、`http2` | false、false；仅 Chat 支持流式；Responses profile 必须使用 `stream=false`，结构化任务也不例外 |
| `request_timeout_sec` | `180`；正数秒。有限时限的 SDK Chat 流式请求也受整体时限约束。 |
| `connect_timeout_sec` | 未设时 SDK TCP/TLS 上限为 20 秒；否则为有限正数秒，且不超过有限请求时限。仅文本 SDK 支持。 |
| `max_output_tokens` | 未设，显式正整数输出上限 |
| `context_input_tokens` | 24000，正整数原生输入工作集目标，不代表提供方上下文容量 |
| `retry_attempts` | 3，至少 1，包含首次物理请求 |
| `connect_retry_attempts` | `1`；可提高已确认发送前建连失败的尝试上限，每次消耗请求预算。 |
| `native_response_corrections` | 严格整数 `0`（默认）或 `1`；仅授权原生 Chat turn 对已结算的截断响应纠正一次。 |
| `retry_base_delay_sec`、`retry_max_delay_sec` | 1、12，正数秒 |
| `reasoning_effort` | 空（不发送）、`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；支持情况取决于提供方和模型。 |
| `thinking_mode` | 空、enabled、disabled、auto；Chat 关闭思考时 effort 须为空或 none |
| `reasoning_output_tokens` | 未设，启用 effort 时的正整数备用上限；显式调用/profile 上限优先 |
| `json_response_format` | auto（默认）、off、json_object；off 不发原生 JSON 参数，仍本地校验 |
| `chat_token_limit_param` | auto（默认）、max_tokens、max_completion_tokens |
| `input_price_per_million`、`output_price_per_million` | 未设，非负费用估计 |
| `image_size` | 未设；auto、256x256、512x512、1024x1024、1024x1536、1536x1024、1792x1024、1024x1792 |
| `image_quality` | 未设；auto、standard、hd、low、medium、high |

### Profile 约束

- 目录严格检查字段和类型。Images profile 只接受图像能力并拒绝文本生成设置；文本 profile 拒绝图像尺寸、质量和生成能力。能力是声明，不是提供方探测。原生决策和 `focus_work` 要求 Chat；`compose_document` 可通过写作路由使用 Responses。预算化视觉检查须在视觉 profile 明确设置 `max_output_tokens`。
- HTTP/2 要求执行主机安装 `simple-autoresearch[http2]`。配置的代理仅作用于该 profile。SDK Chat 流式请求的有限时限覆盖提交与消费；已有 asyncio 循环中可用 `await asyncio.to_thread(client.ask, ...)`。
- 新命名研究／原生任务及聊天草稿固定模型、端点、协议、HTTP/2、推理、纠正授权和相关路由；流式、时限、输出上限、重试和输入目标可调整，同一变量背后的密钥可轮换。明确更换原生文本连接会开始新对话边界，不重置任务或预算。保存的目录路径仅供查找；旧任务沿用原合同，独立 CodeTask 不追溯补绑定。
- `native_response_corrections=1` 要求账本和有限输出上限，只对完整且已结算的 `finish_reason=length` Chat turn 纠正一次；不采用截断文本或执行其中的工具。纠正共用原预算，若新尝试存在未知或预留用量则停止。在保存的连接上启用须明确更换连接。
- 超时或响应中断可能留下未知用量；调整传输设置不会释放预留或恢复失败调用。输出上限和单价估计不保证提供方费用上限。

<a id="全局-env"></a>

## 凭据与环境变量

凭据与部署设置放环境变量或此文件，任务路径/数据/解释器放任务输入或 TOML。目录 profile 优先于旧模型/URL/密钥设置。

| 变量 | 用途／旧默认值 |
| --- | --- |
| `SIMPLE_AR_MODELS_CONFIG` | 模型 TOML 文件路径，共享设置建议使用绝对路径 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`SIMPLE_AR_MODEL` | 旧连接，模型默认 gpt-4o-mini |
| `SIMPLE_AR_LLM_TIMEOUT_SEC`、`SIMPLE_AR_MAX_OUTPUT_TOKENS` | 180 秒，输出上限未设 |
| `SIMPLE_AR_LLM_RETRY_ATTEMPTS`、`SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC`、`SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC` | 3 次，1/12 秒等待 |
| `SIMPLE_AR_LLM_BACKEND`、`SIMPLE_AR_LLM_API` | 旧运输/API；API 默认 responses，原生决策须明确设为 chat；这些变量不覆盖目录 profile |
| `SIMPLE_AR_JSON_RESPONSE_FORMAT`、`SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` | 旧 JSON/token 参数政策 |
| `SIMPLE_AR_LLM_REASONING_EFFORT`、`SIMPLE_AR_LLM_THINKING`、`SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS`、`SIMPLE_AR_LLM_STREAM` | 旧推理/流式控制 |
| `SIMPLE_AR_INPUT_PRICE_PER_1M`、`SIMPLE_AR_OUTPUT_PRICE_PER_1M` | 可选费用估计 |
| `SIMPLE_AR_LLM_MAX_WORKERS` | 共享模型 worker 上限 |
| `OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY` | 独立可选来源凭据，匿名访问仍受额度限制 |
| `TAVILY_API_KEY` | 可选 web 基础搜索/tavily_basic 提取必需 |
| `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS`、`SIMPLE_AR_DOCUMENT_PROXY_ENV`、`SIMPLE_AR_DOCUMENT_CA_BUNDLE` | 关联资料准确主机列表、代理变量名、可选可信 CA 文件 |
| `SIMPLE_AR_RESEARCH_INDEX_ROOT` | 显式清理历史索引的位置，默认 `.simple_ar_cache/research_index`；不控制资料检索后端。 |

关联资料路由使用准确主机名和 TLS 校验，不改变模型、搜索、PDF 或 ZIP 传输。未明确配置路由时，此下载器不使用环境代理。`SIMPLE_AR_OUTPUT_DIR` 由每次进程提供，不是全局设置。

`web` 使用 Tavily basic 搜索；网页提取单独授权和缓存，远程 PDF 另需许可。摘要和元数据是发现资料，不是已核实原文。

## 研究 TOML：分区与默认值

结构化调研可将以下内容保存为 `research.toml`，与 `paper.md` 放在同一目录，
然后运行 `simple-ar research-session --config research.toml`：

```toml
[task]
goal = "比较已有证据及其局限"
kind = "survey"
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true

[assets]
papers = ["paper.md"]
```

参见[调研示例](../examples/survey/README.md)。未知研究分区、字段和类型会报错。带模型的共同创建默认为 30 次请求和 600000 token；无模型时，省略的模型额度为零。续接保留保存的用量和限额。

### 任务、模型与预算

| 字段 | 默认值／约束 |
| --- | --- |
| `task.goal` | 新会话必填 |
| `task.kind` | auto；survey、bug_fix、measurement、reproduction、writing、data_analysis |
| `task.outputs` | summary/report/experiments/bug_fix/code_task/data_analysis 列表，受下表约束 |
| `task.output_root`、`task.selected_idea_id` | runs/research-session；可选有依据的已有候选 ID |
| `model.name` | 文件默认 env，空字符串选择确定性处理 |
| `model.feasibility_review_model` | 可选同提供方独立可行性审查，共享预算，保存后不可改 |
| `model.max_output_tokens` | 可选正整数输出上限 |
| `budget.total_tokens`、`budget.llm_requests` | 带模型的共同创建使用正整数上限，默认 600000 token／30 次请求 |
| `budget.process_invocations`、`budget.process_wall_seconds` | 非负，默认按任务推导；应声明有限的拟执行额度 |

以下限制适用于结构化 `research-session`，不代表普通 `start` 的工具组合范围。

| Kind | 约束 |
| --- | --- |
| survey | 只读文献，除明确代码交付兼容合同外不执行 |
| bug_fix | 已有项目 CodeTask；输出 bug_fix，可加 report；不跑基线 |
| measurement | 明确命令、experiments及可选summary/report（写作须模型）；无CodeTask；可在同一任务使用来源/设计及已核外部基线 |
| reproduction | 下述固定协议，experiments 可加 report |
| writing | 本地材料/论文，仅 report，不执行 |
| data_analysis | 已有表，data_analysis 可加 report，不研究/CodeTask/进程执行 |
| auto | 声明合同与共同动作循环，按需选择能力，不另建固定流程；安装须独立授权 |

明确 outputs 不能与 `--with-report`/`--no-report` 合用。可行性审查是模型判断，不是执行证明。

### 研究输入与行为

| 字段 | 默认值／约束 |
| --- | --- |
| `research.providers`、`research.queries` | 可选列表。内置来源名为 `arxiv`、`openalex`、`semantic_scholar`、`local_files`、`web`；`web` 需要 Tavily 密钥。明确查询作为初始查询保留 |
| `research.max_results`、`research.max_chunks`、`research.idea_limit` | 正整数，CLI 默认 10/300/3 |
| `research.max_pdf_pages` | 未设提取本地全部页，正整数记录截断；改变冻结提取须新会话 |
| `research.read_max_shortlist` | 可选，默认全部且最多24；明确论文保留在限额内，超量报错 |
| `research.cache_dir` | 可选共享全文缓存，不持久化为安全恢复变更 |
| `research.use_fulltext`、`research.allow_pdf_download`、`research.keep_raw_pdf`、`research.materials_only` | false；仅材料关闭搜索，不禁模型阅读；写作始终本地 |
| `research.max_fulltext_documents`、`research.max_pdf_mb` | 可选正上限，引导全文搜索为6份/每份20 MiB |
| `research.web_extract_backend` | direct 或 tavily_basic |
| `research.max_iterations` | 默认1；限制新共同任务的来源收集追加轮次。legacy 科学续接保留保存的合同 |
| `research.interaction` | CLI 默认 checkpoints，可选 assisted/autonomous。新任务用于协议/设计确认；legacy 阶段/交付政策仍归保存的所有者。见[参与策略](CLI_REFERENCE_zh.md#参与策略与待处理决定) |
| `assets.papers`、`assets.materials`、`assets.data` | 书目来源；写作草稿/笔记/结果/包；执行输入。项目内数据复制进隔离准备，外部原地保留，不改 argv 或核划分 |

本地 PDF 不受远程全文关闭影响；远程 PDF 需要下载和保留许可。页数上限记录截断，无法执行上限的解析器会拒绝该设置。搜索保留明确查询；arXiv 用 `AND` 连接普通关键词。模型阅读和自动扩展可在原限额内追加一次搜索、获取和阅读。

### 执行与报告

以下字段属于 `[execution]`。

| 字段 | 约束 |
| --- | --- |
| `command`、`cwd`、`timeout_sec` | 字面 argv、已有 cwd、可选正时限；研究文件 cwd 相对 TOML，CLI 默认 . |
| `code_task_config` | CodeTask 引用，除复现 adapter 外通常不与 command 合用 |
| `primary_metric`、`metrics`、`metric_directions` | 可选结果合同，方向列表如 ["accuracy=higher"]；higher/lower/resource/ignore |
| `output_files`、`metric_sources` | 登记附件／明确有限数值选择器 |
| `pairs`、`seeds`、`seed_flag`、`seed_count` | 明确成对 argv 或紧凑展开，不混用 |
| `baseline` | 可选表，声明控制条件的 `command` 及覆盖字段；未单独声明时沿用主命令 |
| `baseline_policy`、`baseline_ref` | run/skip/reuse；固定测量可引用已通过的同条件 canonical 结果文件，配对条件可提供按条件顺序排列的路径列表 |
| `protocol` | contract_id/hypothesis、dataset_refs、split_spec、metric_specs、comparison_conditions、protected_assets；复现还需 dataset/expected_outcome |
| `initial_files` | 须显式 CodeTask 准备；明确允许、尚不存在、不受保护的 Python 路径，不限复现任务。新任务交由 CodeTask 直接创建，不先写占位实现；已保存 legacy 合同保留原语义。 |

仅文献任务省略 command 和 CodeTask。使用 `baseline_policy="reuse"` 时，`baseline_ref` 相对配置文件解析（无配置文件时相对 cwd）。原结果必须通过，且命令、schema、协议和保护文件哈希须匹配；原结果保留为只读快照，仅运行候选条件。CodeTask 也可使用已核外部基线，但不能把它当作自身准备的产物。

| `[execution.environment]` 字段 | 默认值／约束 |
| --- | --- |
| `mode` | venv 创建任务环境；current 必须明确检查，不安装或替换解释器；普通 current 执行省略此表 |
| `requirements`、`install_project` | []、false；requirements 相对执行 cwd |
| `python_executable` | venv 基础解释器，默认运行时 |
| `timeout_sec`、`check_command` | 每步300秒，可选字面短检 argv |

准备和检查共享进程预算，失败则停止测量。任务 venv 解析裸 Python 别名，冲突的显式执行器会被拒绝。安装可能运行构建代码或联网；工作区不是 OS 沙箱。

若声明源码基线，必须在实施前完成测量；空项目不能虚构基线。`skip` 只运行声明的候选。普通验证与测量可以使用相同 argv，但必须分别调用；复现要求不同的验证 argv。

~~~toml
[execution.output_files]
runtime = "runtime.json"
observations = "tables/observations.csv"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "observations", column = "coverage", match = { method = "candidate" } }
~~~

附件须为 `SIMPLE_AR_OUTPUT_DIR` 下不超过 2 MiB 的 UTF-8 普通文件，以相对 POSIX 路径登记。字面参数 `{output_dir}` 绑定该目录。登记文件可预览和读取；缺失或超限文件不能支撑结论。`results.json.output_evidence` 记录来源和截断。读取可指定字符窗口、字面匹配，或用最多四个精确标量条件筛选 JSON/CSV/TSV 行。

指标选择器必须找到有限数值：JSON 使用明确键或下标，CSV/TSV 必须唯一一行。缺失、歧义或与 stdout 冲突会失败；文件不能使失败进程变为成功测量。

#### 固定协议复现

设置 `materials_only=true`、本地论文、命令、有限时限、`baseline_policy="skip"`、假设、数据集和预期结果。可选 CodeTask 准备范围内 adapter/入口；其 benchmark 须先验证通过，且解析解释器后 argv 与测量不同。初始文件须明确允许且尚不存在。不会隐含变更方法、划分或扩展种子；环境检查在实施前进行。任务 venv 要求 CodeTask 初始使用 current Python，不能与源 venv 复用组合。见[复现示例](../examples/conformal_reproduction/README.md)。

#### 配对与种子

每个 `[[execution.pairs]]` 须提供唯一整数 `seed`，以及两个字面 argv 键 `baseline_command`、`candidate_command`：

~~~toml
[[execution.pairs]]
seed = 0
baseline_command = ["python", "evaluate.py", "--method", "baseline", "--seed", "0"]
candidate_command = ["python", "evaluate.py", "--method", "candidate", "--seed", "0"]
~~~

另一方式使用 `command`、唯一 `seeds` 或正数 `seed_count`，以及明确的 `seed_flag`；不能与 `pairs` 混用。已有种子参数会被替换。单个 flag 只登记一个种子；协议内 seeds 仅描述条件，不展开命令。追加配对需要分析建议、接受决定和剩余进程额度。

基线复用核对命令、数据、划分、指标、条件、准备与声明的保护文件。每个保护文件须有唯一 `asset_id` 和路径，路径相对执行 cwd 或为绝对路径。每次调用都须计入预算；直接 argv 使用进程 PATH，CodeTask 按环境规则解析裸 Python。

## 已有材料写作

结构化写作使用 `kind="writing"`、`outputs=["report"]`、本地材料或论文及模型。普通 `start --kind writing` 创建原生任务。来源放在 `assets.papers`，草稿、笔记和结果放在 `assets.materials`；重复或双重角色路径会报错。支持文本、Markdown、HTML 和 PDF。普通 JSON 是未核实材料；无效 JSON 和损坏的 `table_analysis.*` 包会失败。

导入 `table_analysis.v1` 须保留相邻数据副本，JSON 和输入各不超过 20 MiB；系统复算并重建图形。请在目标中说明文体、长度及计数范围，并区分目标与硬性范围。

| `[report]` 字段 | 默认值／行为 |
| --- | --- |
| `writing_method` | 新 article；整稿独立检查及有界纠正由其所有者承接；明确分节/自适应控制用 sections；保存方法不暗换 |
| `template` | auto；source_review/survey/material_report/experiment/reproduction/analysis_report 或 Markdown 模板 |
| `reviewer`、`max_review_iterations` | Article 默认不审阅；明确 llm、整稿审阅/范围时，0轮只检查，正数N轮最多提出N个完整候选，每个候选独立检查后才采用 |
| `document_review`、`review_scope` | Sections：false、section；document 范围须开启整稿审阅 |
| `draft_scope` | section；document/auto 要求整稿审阅/范围及 full 来源策略 |
| `outline_strategy` | auto 使用建议结构；template 固定正文一级章节及顺序；明确自定义模板保留其计划结构；adaptive 须选 sections |
| `data_tables` | linked；full 另含分析表／实验摘要表 |
| `max_section_tokens` | 0/未设无单次上限；正数按方法限制单节/联合/整篇 |
| `max_cited_sources` | 未设/0无上限；正数限制正文引用的不同来源，不缩减证据池；整稿与最终装配均检查（含图注），不靠删除引用或自动重写解决超量 |
| `max_document_review_prompt_chars` | 0无附加完整请求上限；正数超量拒绝不截断；已有60000字符稿件/检索窗口仍生效 |
| `figures` | report.figures.enabled=false 或 mode="off" 关闭生成；max_figures=0自动，正数总上限含导入图，超量失败 |

### 写作约束

- `article` 用一次请求起草完整正文。独立检查需设置 `reviewer="llm"`、`document_review=true`、`review_scope="document"`；纠正轮数为零时只检查不改写。采用前会核对必需标题和引文上限。
- `sections` 保留计划和检查点。`draft_scope="auto"` 仅在每节都有正长度目标且总数不超过 2,000 词时联合起草，否则分节起草。分节补读使用现有访问权限和工具额度。
- Auto 模板根据来源和任务选择 `source_review`、`survey`、`material_report`、`experiment`、`reproduction` 或 `analysis_report`。引导设置可明确不同于基础默认值。两种表格模式都保留可搬迁证据包；图形须有材料依据。
- 续接沿用保存的写作方法与约束。检查和算术复算不认证科学正确性。

<a id="已有材料写作的章节规划"></a>

## 已有数据的描述分析

~~~toml
[task]
goal = "描述已有测量"
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
observation_unit = "一次运行"
group_column = "method"
value_unit = "分"
~~~

| `[analysis]` 字段 | 默认值／约束 |
| --- | --- |
| `file`、`value_columns`、`observation_unit` | 必填 UTF-8 CSV/TSV/JSON记录路径、非空数值列列表、非空行语义 |
| `group_column`、`value_unit`、`x_column`、`x_unit`、`attribution`、`paired_baseline` | 默认空，单位未知；attribution 仅声明，不获取或核书目 |
| `mode`、`plot`、`missing`、`width` | 分别默认为 `observations`、`bar`、`reject`、`wide`；缺失策略可选 `omit`，宽度可选 `column`。图形与模式的组合见下表 |
| `association`、`series_layout` | 分别默认为 `none`、`separate`；坐标图可选 `pearson` 相关与 `shared` 共用坐标轴 |
| `max_mb`、`max_points`、`max_figures` | 分别默认 20 MiB、10,000 个位置或单元格、100 页图。均可调整为正数；超限会报错，不自动抽样 |

分析时检查有限数值并保存输入字节与设置。缺失值不会填为零；改变变换需要新任务。仅分析不使用模型；追加报告需要模型和共享预算，附加本地材料不授权搜索。

| 图 | 模式／行为 |
| --- | --- |
| `bar` | `observations` 计算分组计数、均值、样本标准差与四分位数；`values` 保留给定值，并要求标签唯一 |
| `box` | 仅用于 `observations`，显示四分位数、中位数和实际最小/最大值；四分位数按 `(n−1)p` 插值，不接受坐标或 Pearson 选项 |
| `heatmap` | 使用 `values`，要求唯一行标签及声明的共同量和单位。跨页共用原值色标，不归一化、排序或聚类；`omit` 保留缺失格，全缺失矩阵报错。不接受坐标、Pearson、配对或 shared 选项 |
| `line` | 使用 `values` 和数值 x；每组 x 须唯一、完整，并按 x 排序；缺失 y 时断线 |
| `scatter` | 使用 `values` 和数值 x；保留重复坐标。`omit` 会在数据包保留缺坐标行，但不绘制这些行 |

箱线图须表示实际最小值与最大值，不是置信区间或 Tukey 界。坐标不自动拟合或聚合。共用坐标轴须声明共同单位；数量上限包含所有系列和热图缺失格。

`paired_baseline` 要求 observations/bar 或 box，同时选择基线与候选，并确认同一行匹配及共同量和单位。它对完整配对计算候选减基线，保留数量、均值、样本标准差和标准误。至少两个独立配对时，±1 标准误不等于置信或显著性结论。箱线图单列分布使用各列非缺失行，配对差值使用完整配对。

`pearson` 要求 line/scatter 坐标，按组和列使用共同非缺失的 x/y 配对。常量轴或不足两对时未定义，两对非恒定值必得 ±1；不推断回归、因果或独立性。见[曲线示例](../examples/data-curves/README.md)。

## 研究会话补齐条件与续接

### 准备执行后的续接

新共同任务使用 `start --resume-task SESSION`；文字修改使用反馈，补齐或替换合同使用[明确补齐条件](CLI_REFERENCE_zh.md#原生任务恢复)。保存的 ResearchApplication 会话使用 `research-session --config research.toml --session-root SESSION`。

对这些保存的会话，省略的目标和输出沿用；明确修改目标、论文或执行条件会重建相关计划。测量复用须匹配命令、schema、协议、准备和保护文件。不兼容的任务类型或提取变更需要新会话；仅修改报告不会重跑测量。`research-report` 可补写报告。

使用 `continuation.decision_id`、`decision_response`（`accept`、`reject`、`revise`）和 `decision_guidance` 答复待处理决定，交付修订还需报告设置。输入或条款变更会使旧决定失效；交互变更不能与重分析或单独报告刷新混用，答复交付决定除外。

### 显式续接额度

~~~toml
[continuation]
authorization_id = "continuation-1"
reason = "完成保存目标"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
~~~

此表用于带 `--session-root` 的保存会话；新共同任务使用[原生续额参数](CLI_REFERENCE_zh.md#原生任务恢复)。`remaining` 设置后续容量，`additional_attempts` 与 `additional_no_progress` 增加上限但不清零用量。须提供稳定 ID 和原因：相同 ID 与条款只补完中断的授权，不补回已消耗额度；改变条款需新 ID。单独授权不会重启已完成任务或刷新报告；若后续需要执行，应在创建时声明进程预算维度。

## 独立 Code-Task Config

~~~toml
[code_task]
kind = "existing_project"
code_root = "{config_dir}/project"
task_file = "{config_dir}/task.md"

[benchmark]
command = "python main.py"
primary_metric = "accuracy"

[benchmark.metric_directions]
accuracy = "higher"

[workspace]
mode = "copy"

[edit_scope]
allowed_patterns = ["src/**", "main.py"]

[execute]
to_step = "propose-edits"
~~~

先运行 `code-task init --config PATH`，再运行 `code-task execute RUN_DIR --config PATH`。

Greenfield 设置 kind="greenfield"。没有脚手架时省略 code_root，并声明真实生成项目的 benchmark 命令。

外部 handoff 选择受支持的 provider，设置 agent_mode="handoff" 和 allow_external_agent=true。候选文件仍须经过正常审阅、验证及 benchmark。

## 加载规则

`code-task init --config` 读取初始化设置，`execute --config` 读取执行、模型和预算设置；`research-session --code-task-config` 复用同一解析器。明确 CLI 参数优先于文件设置。

CodeTask 相对路径从命令工作目录解析。源码、任务、输出、解释器、benchmark 命令、基线文件和 `required_paths` 可用 `{config_dir}` 引用配置文件所在目录。不要使用资源环境变量插值；命令中含空格的路径须正确引用。`required_paths` 只检查路径是否存在。

注意：多数 CodeTask 分区仍会忽略未知键，拼写错误可能不生效；`workspace` 和 `ablation` 会拒绝额外字段，旧阶段选择也会报错。预算分区接受其支持的覆盖设置。

当前公开解析器不读取旧的 `implementation.max_repair_attempts`，请使用 `execute.repair_rounds`。旧外层流水线配置仅用于理解历史记录。

## CodeTask TOML 字段参考

### Code-Task 字段

以下表格按 TOML 分区列出字段。选择解释器不会安装依赖。外部代理生成的文件仍须经过正常审阅、静态验证、运行和结果检查。

| 分区 | 字段 | 作用与默认值 |
| --- | --- | --- |
| `[code_task]` | `kind`、`code_root`、`task_file`、`output_root`、`name` | 默认修改已有项目（`existing_project`），须提供源项目和任务文件。输出根目录默认为 `runs`，名称后缀可省略。`greenfield` 必须有任务文件，可选提供脚手架，生成项目位于 `workspace/generated_project`。 |
| `[code_task]` / `[safety]` | `max_file_bytes` | 限制 copy/sparse 模式复制的单文件大小，默认 2,000,000 字节。code_task 的设置优先于 safety；`0` 表示关闭该限制。 |
| `[benchmark]` | `command`、`timeout`、`primary_metric`、`metric_directions` | 指定工作区内运行的命令字符串、可选时限、主指标和方向表。 |
| `[metrics]` | `primary`、`primary_metric`、`directions`、`metric_directions` | 保留兼容字段。benchmark 的主指标优先；方向表依次合并，后面的表和明确 CLI 参数覆盖前面的值。 |
| `[environment]` | `mode`、`python`、`python_executable`、`required_paths` | `current` 使用当前解释器；`external` 必须指定 Python，`python` 优先于其别名。只替换命令开头的裸 Python，其余 argv 保留。required_paths 默认为空，只检查声明的路径是否存在。 |
| `[workspace]` | `mode`、`include`、`exclude`、`reuse_source_venv` | 已有项目默认 auto，greenfield 默认 empty。include/exclude 指定稀疏复制规则；reuse_source_venv 默认 false，启用后可使用检测到的源项目虚拟环境解释器。 |
| `[edit_scope]` | `allowed_patterns`、`protected_patterns`、`mode` | 分别指定允许编辑的 glob、额外只读 glob 和记录标签。标签本身不授予权限。 |
| `[safety]` | `validation_max_file_bytes` | 静态扫描默认最多读取单文件 500,000 字节，execute 的对应设置优先。 |
| `[implementation]` | `provider`、`agent_mode`、`allow_external_agent` | provider 默认 local，还支持 fake、local_llm、codex、claude_code、opencode、external_cli。支持 model 和 handoff；delegated_workspace 会明确失败。外部启动必须明确授权。 |
| `[implementation]` | `agent_model`、`agent_binary`、`agent_args`、`agent_timeout_sec` | 可选的模型、程序和参数；时限默认 600 秒。模型留空时使用外部 CLI/账号默认值。 |
| `[resource]` | `max_runtime_sec`、`max_files`、`max_generated_lines`、`max_memory_mb`、`allow_gpu` | 提供规划和代理交接时的资源声明。运行时限、文件数和行数还供 execute 回退使用；这些字段不提供操作系统隔离，也不安装依赖。 |

静态检查遵守 Python 源码编码。若文件已确认与冻结 Git 提交的字节一致，其已有语法缺陷在非 strict 模式下保留为警告；新建或改坏的文件，以及无法核实的基线，仍作为错误。

<a id="execute-与-budget"></a>

### Execute 与 Budget 字段

| 分区 | 字段 | 作用与默认值 |
| --- | --- | --- |
| `[execute]` | `to_step`、`use_llm` | 默认最多推进到 run，并启用模型。可选停止点见 [CLI](CLI_REFERENCE_zh.md)。 |
| `[execute]` | `model` | 通用模型选择，依次回退到 models.code_task.default、models.default、llm.model。 |
| `[llm]` | `enabled`、`model` | 保留兼容默认值；execute 的明确设置优先。 |
| `[models.code_task]` | `planner`、`writer`、`reviewer`、`editor`、`repair`、`summarizer` | 分别用于规划、greenfield 文件生成、审阅、编辑提案、修复和总结。未明确选择时使用通用连接。 |
| `[execute]` | `timeout_sec`、`timeout` | 优先使用 timeout_sec，其次是 timeout 别名、benchmark.timeout、resource.max_runtime_sec，最后默认为 60 秒。 |
| `[execute]` | `skip_validation`、`strict_validation`、`validation_max_file_bytes` | 两个开关默认 false，静态扫描单文件上限默认 500,000 字节。 |
| `[execute]` | `stream_benchmark_output` | 默认 off/false；line 转发换行日志，auto/true 同时处理回车进度，summary 只显示结束后的尾部。 |
| `[execute]` | `baseline_policy`、`baseline_metrics_file` | 默认 auto。run 强制基线，skip 跳过，none 表示无比较，provided 记录用户提供值并标明不是本次复测。 |
| `[execute]` | `apply_proposed_edits`、`allow_large_edits`、`allow_planning_fallback` | 默认 false；分别允许应用已审提案、较大编辑、符合条件的离线回退。 |
| `[execute]` | `planning_mode`、`planning_review_rounds` | 默认 compact 和 0。tool_agent 是弃用别名；旧审阅轮数不会授予调用。 |
| `[execute]` | `llm_retry_attempts`、`repair_rounds` | 完整响应纠正默认最多 3 次（含首次），技术修复默认 0 轮；运输重试另由连接配置控制。 |
| `[execute]` | `max_files`、`max_source_chars_per_file`、`max_generated_lines` | 默认 8、4,000、1,600；文件数和行数可回退到 resource 设置。完整规划超限会失败，不丢弃模块，也不据新上限重建旧计划。 |
| `[execute.ablation]` | `review_gate` | 默认 strict；runtime 只阻塞安全和直接可执行问题，允许保留包装或报告缺口。 |
| `[execute]` | `budget_profile`、`max_batches`、`cost_cap_usd` | 覆盖 budget 分区的对应设置。 |
| `[budget]` | `profile`、`max_batches`、`cost_cap_usd` | 默认 normal 编辑预算；可设置批次数量和有费用估计时的成本上限。 |
| `[budget]`、`[budget.normal]`、`[budget.large]` | `max_files`、`max_edits`、`max_old_chars`、`max_new_chars`、`max_total_edit_chars`、`max_proposal_chars` | 限制提案的文件数、替换数、单次旧/新字符数、总编辑字符数和序列化大小。所选 profile 表覆盖顶层数值。 |

连接重试、研究额度、进程限制、编辑大小和纠正轮数各自独立。Normal 默认最多修改四个允许文件，总编辑字符数为 12,000。Large 必须明确批准，且仍受绝对上限约束；单源码块上限为 32,000 字符。Provided 指标 JSON 可使用平数值映射，或 metrics、metric_values 包装。命令运行成功不等于目标改善。

#### 规划失败与修复

完整但格式错误的规划响应可以在限额内纠正或明确回退；提供方故障和不完整响应不能这样处理。离线 greenfield 回退是四文件开发骨架，不是完成的实现。技术修复需要审阅，并遵守已接受的协议、范围和进程额度。

#### 历史规划

保存的已接受计划保留文件与用量。退役阶段路由会被拒绝；生成项目和导入的已接受计划不重新规划。

## 工作区模式

研究准备保留配置的复制上限、稀疏规则和源 venv 策略。源 venv 复用不能与托管任务环境组合。源码清单跳过符号链接和 Windows reparse 条目；工作区隔离不是 OS 沙箱。

### `auto`

这是已有项目的默认模式。可行时创建 detached Git worktree，否则使用受保护的复制，并记录原因。研究准备遇到未提交源码时使用 copy；仓库中的子目录项目使用 worktree 中对应的子目录。

### `copy`

复制符合规则的当前工作树文件，可以包含未提交源码。排除规则和大小限制可能造成遗漏，应检查准备记录。

### `git_worktree`

使用已提交的 HEAD 创建 detached worktree。源项目须在至少有一次提交的本地 Git 仓库中，不要求远程仓库。显式选择后若无法创建就失败，不降级为 copy，也不包含未提交源码。

### `sparse_copy`

独立 CodeTask 按 include/exclude 规则复制，同时排除 Git、虚拟环境、runs、缓存、构建、数据、模型和私密路径。此模式可能遗漏运行依赖。

研究准备支持 auto、copy、git_worktree 和 sparse_copy；声明的项目数据不能通过 sparse_copy 绕过复制保护。Greenfield TOML 默认 empty；采用 copy/sparse_copy 脚手架模式必须提供 code_root。CLI 的 `--workspace-mode` 不接受 empty。

<a id="edit-scope-behavior"></a>

## 编辑范围规则

文件修改只发生在 `code_task/workspace` 内。`allowed_patterns` 为空时，规范化且不受保护的路径可以编辑；非空时，文件必须匹配至少一个允许规则，同时不能匹配任何保护规则。

测试、benchmark、`.env`、秘密和凭据路径的内置保护始终保留。用户设置的 `protected_patterns` 追加保护，不替换默认规则。工作计划或当前批次的目标文件还会进一步缩小可编辑范围。

上下文选择、规划、编辑提案和修复均遵守范围。应用提案前再次检查工作区路径、范围、唯一精确的旧文本匹配和预算。空 `old` 只用于创建尚不存在且允许的文件，不能覆盖已有文件；新建文件沿用快照和失败回滚机制。

## 在研究会话中使用 CodeTask

使用 `execution.code_task_config` 或 `--code-task-config` 引用 CodeTask 文件。CodeTask 负责编辑和验证，研究负责测量和报告；复现 adapter 还可声明独立测量命令。明确编辑上限在研究和恢复中仍生效。

准备、实施和测量共用任务权限与进程限制。要求报告时，须使用已接受的实施证据和当前稿件。Greenfield 范围和预算适用于完整候选。涉及 checkout 过滤、attributes、子模块或换行转换时，请选择 `copy` 或保留专家 worktree 协议。

## 可信本地扩展

只选择可信包。V1 支持说明包和声明 `read_only` 或 `plan` 的 Python 工具；
Python 拥有宿主进程的操作系统权限，没有 OS 沙箱。

```toml
[task]
extensions = ["../my-skill"]
```

在原生任务 TOML 中设置，使用 `simple-ar start --task-config path/to/task.toml`。
每项是包含 `extension.toml` 的包目录或描述文件，相对任务配置解析。
必须明确选择，不会自动发现、下载或安装。可只提供说明，也可只提供工具。

| 作者声明 | 含义 |
| --- | --- |
| `[extension]` `id`、`version`、`description`、`scope` | 必填非空元数据；ID 是唯一命名空间，匹配 `[a-z][a-z0-9_]{0,23}`。 |
| `instructions`、`resources` | 可选的包内说明文件与辅助文件列表；文件必须位于包内。 |
| `[[tools]]` `name`、`description` | 必填工具名和描述；名称遵循 ID 格式，公开名称为 `id__name`，`instructions` 保留。 |
| `implementation`、`function` | 明确独立 `.py` 文件和 Python 函数标识符；不支持包相对导入。 |
| `permission_level` | `read_only` 或 `plan`；作者须保证工具确定且无副作用。 |
| `input_schema` | 必填对象 schema，按 JSON Schema Draft 2020-12 校验；支持本地 `$defs`/`$ref`，禁止远程和 `file:` 引用获取。 |
| `completion` | 可选 `{ kind = "delivery", field = "handoff", closing = true }`；见下文。 |

工具实现 `function(arguments, services) -> dict`，返回 JSON 对象。
`services["call_tool"](name, arguments)` 提供受宿主策略检查的授权挂载读取／计划操作，
不授予任意写入、进程、密钥或网络服务。Python 本身仍可凭宿主权限直接读写文件、
启动进程或联网。说明正文只在调用 `id__instructions` 时进入上下文；加载工具 schema
不会同时加载正文。

可选 `completion` 的 `kind` 默认 `delivery`，`field` 默认空。非空字段须出现在输入
schema 中并包含交付对象（`summary`，可选 `artifacts` 和 `unresolved`），结果在同一键返回。
字段为空时，整个参数和结果就是该对象。`kind="question"` 使用整个
`{question = "..."}` 对象。`closing=true` 仅允许已可见且无需后续模型工作的工具
用于最后一次决定，不授予权限或额度。

续接和执行要求原描述文件、代码、说明与声明的资源保持原路径和哈希；文件缺失或
变更会阻止使用，须恢复原包。未声明的依赖不在身份检查范围内；模块全局变量
不会跨续接持久保存。

参见示例[描述文件](../examples/local_extension/extension.toml)、
[说明](../examples/local_extension/SKILL.md)和 [Python 工具](../examples/local_extension/notes.py)；
加载细节见 [`extensions.py`](../src/simple_ar/tools/extensions.py)。

## Python 创建任务

结构化调研、写作、表格和声明执行使用 `create_research_task`，与 CLI 共用输入校验，返回 `ToolApplication`；创建本身不调用模型或进程。直接集成原生任务可用 `ToolApplication.create`。

### 创建、执行与恢复

```python
from pathlib import Path
from simple_ar.app.research_intake import create_research_task
from simple_ar.app.tool_application import ToolApplication
from simple_ar.research.workflow_contracts import ResearchBrief

app = create_research_task(
    ResearchBrief(request_text="解释材料中的证据及其限制。",
        requested_outputs=("summary",),
        asset_requests=({"locator": "notes.md", "role": "material"},)),
    root=Path("runs/evidence-note"), model="route:default",
)
app.run(progress=print)
restored = ToolApplication.load(Path("runs/evidence-note"))
```

路径相对 `ResearchApplicationServices.input_base_dir`，不是任务输出目录。Services 提供研究设置和有限的 `budget_limits`，显式零额度保持为零。不传 `model` 时由调用方提供动作，后续可明确补齐。请求报告保留交付义务，不授权模型调用。来源别名、环境及已核外部基线与 CLI 共用输入编译。

```python
from simple_ar.app.research_application import ResearchApplicationServices

services = ResearchApplicationServices(input_base_dir=Path.cwd(), config={
    "execution": {"command": ["python", "experiment.py"], "cwd": ".", "timeout_sec": 120,
        "result_schema": {"primary_metric": "score", "required_metrics": ["score"]}}},
    budget_limits={"process_invocations": 1, "process_wall_seconds": 120})
app = create_research_task(ResearchBrief(request_text="测量所提供的程序。",
    requested_outputs=("experiments",)), root=Path("runs/measurement"), services=services)
```

声明命令或条件集合放入 `services.config["execution"]`。可选 `code_task` 保留 CodeTask 默认值、编辑边界和独立验证。执行须单独批准实际提案；`execution_confirmation=True` 另要求接受冻结协议。CodeTask 的 `allowed_patterns` 未填或为空时保留默认源码范围及强制保护，要缩小范围请给明确 glob。

需要研究而未声明比较控制时，设计保持开放，不隐含 `baseline_policy="skip"`。接受的设计分别绑定未改源码基线和实现候选，不能扩大范围、替换环境或给空项目虚构基线。无模型的实验任务可以先保存材料并暂停等待[明确补齐条件](CLI_REFERENCE_zh.md#原生任务恢复)，无论是否已声明命令；收集材料不等于完成。首次绑定模型须在原账本明确追加正数请求和 token 额度。

### 原生控制与兼容性

保存任务沿用其控制设置。以下设置用于 Python 集成：

| API 设置 | 行为 |
| --- | --- |
| `delivery_files=["**/*.md", "**/*.pdf"]` | 明确的相对模式要求完整交付包含非空工作区输出；存在不等于质量。 |
| `tool_contracts="on_demand"` | 创建默认值。先提供共同工具，通过 `load_tools` 加载可选合同；`full` 显示全部已允许合同。加载不授予权限。 |
| `context_protocol="auto"` | 创建默认值，新操作任务使用 `working.v4`，共用输入预算。省略 `update_work_plan.notes` 时保留，`[]` 清空。 |
| `context_protocol="working.v1"`、`"conversation.v1"`、`None` | 兼容选项沿用保存的选材和恢复规则。 |
| `control_protocol="operations.v1"` | 新任务在一个循环中组合允许的操作；保存任务使用 `load` 和 `supply`。 |
| `completion_policy="available_operations.v1"` | 在权限和资源限制内保留可用操作；耗尽时保留成果并暂停。 |
| `report={"status":"canonical","manuscript":"report.md","formats":["markdown","pdf"]}` | 声明可编辑正文与输出格式；`delivery_review_protocol=task.v1` 独立检查当前工作和成果清单，出版物须与正文匹配。 |
| 交付中的 `limitations`、`unresolved` | 分别记录不确定性和未完成的必需工作；后者使交付为部分完成。 |
| 交付中的 `summary_source={"path":"report.md","offset_bytes":0,"max_bytes":1024}` | 以交付文件中绑定字节的 UTF-8 窗口作为摘要；来源无效或变化会失败。 |
| `create_research_task(..., allow_vision=True)` | 默认 `False`；要求命名视觉路由和共享 `services.budget_limits`，不授予图像生成或来源联网。 |
| `supply(..., replace_existing=True)` | 明确替换 `delivery_files`、`tool_contracts` 或 `completion_policy`；其他补齐保留策略和账本。 |

`inspect_artifacts` 接受可解码文本，图片使用 `inspect_images`。目标变化须重新检查。可跨已授权目录引用本地图；出版绑定图像字节，图像变化须重新批准。冻结的 `report_requirement` 保留正文、提纲和引用限制；`target_words` 是目标。

### 历史写作合同、输出绑定与保留成果

这些选项用于显式兼容集成和保存的专业任务。新 `operations.v1` 任务直接编辑、检查和导出正文，不使用 `report_protocol`、`professional_inputs` 或 `update_work_plan.report_intent`。

| 操作／字段 | 行为 |
| --- | --- |
| `compose_document.professional_review={"max_revision_rounds":1}` | 独立检查后最多纠正一轮，零表示只检查。冻结设置优先。 |
| `compose_document.prior_result` | 以原证据和检查继续已检查但未采用的修订；未解决的必需意见阻止认可出版。 |
| `compose_document.adopt_draft=true` | 不起草而检查和采用 `prior_draft`；须启用整篇写作与独立专业审阅。`reuse_prior_inputs` 要求完整同任务生产记录。 |
| `run_process.inputs`、`run_process.outputs` | 可选绑定，各最多 64 项、每项最多 64 MiB。审批绑定输入，成功执行记录输出；绑定变化须重新调用。 |
| `app.run()`、`app.result_snapshot()` | 提供 `retained_work`，包括中间文件和续接说明；不代表已接受交付。 |

保存的写作合同继续使用原审阅、证据和预算规则。已有图形、反馈和附件仍是证据输入；检查不认证科学正确性。

### 可信服务、快照与兼容 API

Services 可注入文本客户端、独立审阅客户端和 `SearchProviderRegistry`。客户端实现 `with_budget` 及 `ask_json` 或 `chat_turn`；使用 `ToolApplication.load(path, services=services)` 恢复。持久化保存身份，不保存密钥或可执行对象；缺失服务会失败，不自动回退。自定义实现由调用方信任，不是沙箱。

`snapshot_sources=True` 保存选定普通文件及其哈希，每个受 `max_snapshot_bytes`（默认 16 MiB）限制。旧 v1 导入保留父任务关系，历史预算记为 `unknown_not_imported`。`create_session` 和 `ResearchApplication.create` 是兼容 API，沿用保存的 `view` / `advance` 合同。
