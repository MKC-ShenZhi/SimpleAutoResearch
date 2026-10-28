# 配置参考

[English](CONFIG_REFERENCE.md) · [使用指南](USAGE_zh.md) · [CLI 参考](CLI_REFERENCE_zh.md)

<a id="文件优先级与生效时机"></a>

## 配置文件导航

按你要修改的文件查阅下面各节，正文与表格顺序一致。日常使用通常只需配置密钥和模型连接；希望预先指定材料、权限或预算时，再使用任务 TOML。高级文件仅在对应入口中使用，无须逐份配置。

| 文件／输入 | 管什么、在哪里使用 | 修改后何时生效 |
| --- | --- | --- |
| [`.env`／环境变量](#environment-file) | API 密钥、模型配置文件位置及部署选项；`.env` 放在运行命令的当前目录。 | 下次命令或客户端读取时；已有环境变量优先。 |
| [`models.toml`](#model-file) | 可复用模型连接及用途路由；可放用户配置目录或显式指定位置。 | 新选择连接时；已有任务切换连接需显式操作。 |
| [`task.toml`](#task-file) | 普通 `start` 任务的目标、材料、权限和资源上限。 | 创建新任务时；不修改已运行任务。 |
| [`research.toml`（高级）](#research-file) | `research-session --config` 的结构化输入，包含检索、执行、分析和写作设置。 | 创建时使用；已有任务按续接规则处理。 |
| [`code-task.toml`（高级）](#code-file) | 独立代码任务的项目、工作区、编辑范围和执行设置，也可被研究配置引用。 | 初始化工作区或下次执行时，取决于字段。 |
| [`extension.toml`（高级）](#extension-file) | 自定义本地扩展包的描述文件；仅在显式加载扩展时使用。 | 选择扩展时；已有任务保留原版本约束。 |
| [已保存任务目录](#saved-task) | 系统维护的进度、成果、用量和恢复记录。 | **不是可编辑配置文件**；通过续接命令调整。 |

`task.toml`、`research.toml`、`code-task.toml` 是本页示例文件名，可自行命名。它们属于不同入口，不能把所有表合并成一份通用任务配置。语言、篇幅和交付要求可以直接写在任务目标中。

<a id="environment-file"></a>

<a id="凭据与环境变量"></a>

## `.env`：凭据与环境变量

<a id="全局-env"></a>

密钥放环境变量或私有 `.env`，不要写进共享 TOML。`start`、`models` 和模型客户端读取运行命令的当前目录中的 `.env`；系统已有环境变量优先。模型 TOML 放在别处，也不会改变 `.env` 的读取位置。下表均在对应读取方下次读取时生效，不重置已有用量。目录中的 profile 提供自己的模型与传输设置，不受旧环境连接设置覆盖。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `SIMPLE_AR_MODELS_CONFIG` | 共享模型目录文件 | 路径字符串；省略时使用用户目录 | 下次加载目录 |
| profile 的 `api_key_env` 指定变量 | 连接凭据 | 必需的非空秘密字符串 | 下次创建客户端；同名变量可更换密钥值 |
| `SIMPLE_AR_LLM_MAX_WORKERS` | 模型并发上限 | 正整数；省略时使用请求的并发数 | 下次并行操作 |
| `OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY` | 可选检索密钥 | 字符串；省略时匿名访问，受匿名配额约束 | 下次连接器加载 |
| `TAVILY_API_KEY` | web 检索／tavily_basic 提取密钥 | 使用这些服务时必需的字符串 | 下次连接器加载 |
| `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS` | 精确匹配的关联文档路由主机 | 逗号分隔字符串；未设 | 下次文档下载 |
| `SIMPLE_AR_DOCUMENT_PROXY_ENV`、`SIMPLE_AR_DOCUMENT_CA_BUNDLE` | 代理变量名／可信 CA 路径 | 可选字符串 | 下次路由下载 |
| `SIMPLE_AR_RESEARCH_INDEX_ROOT` | 历史索引清理位置 | 路径 .simple_ar_cache/research_index | 显式清理；不是检索后端选择 |

关联文档路由使用精确主机与 TLS 校验，不配置模型、检索、PDF 或 ZIP 传输。没有显式路由时，该下载器忽略环境代理。`SIMPLE_AR_OUTPUT_DIR` 由应用逐进程提供，不是应编辑的全局设置。

### 仅使用环境变量的兼容连接

已经使用 `models.toml` 时，跳过本节；这些变量只配置未使用模型目录的旧连接。
新配置优先使用上面的命名连接，避免在两处重复填写模型和时限。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`SIMPLE_AR_MODEL` | 无目录时的旧连接 | 字符串；密钥必需、URL 未设、模型 gpt-4o-mini | 下次创建兼容连接 |
| `SIMPLE_AR_LLM_API`、`SIMPLE_AR_LLM_BACKEND` | API／传输方式 | 字符串 responses、openai；API 另支持 chat/auto，后端另支持 litellm | 下次创建兼容连接；原生决定需 Chat |
| `SIMPLE_AR_LLM_TIMEOUT_SEC`、`SIMPLE_AR_MAX_OUTPUT_TOKENS` | 旧请求时限／输出上限 | 正数 180 秒；可选正整数上限 | 下次创建兼容连接 |
| `SIMPLE_AR_LLM_RETRY_ATTEMPTS` | 请求总尝试次数 | 整数 ≥1；3 | 下次创建兼容连接 |
| `SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC`、`SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC` | 旧重试延迟 | 正数 1、12 秒 | 下次创建兼容连接 |
| `SIMPLE_AR_JSON_RESPONSE_FORMAT`、`SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` | 旧 JSON／token 参数策略 | 字符串 auto、auto；选项见模型连接字段表 | 下次创建兼容连接 |
| `SIMPLE_AR_LLM_REASONING_EFFORT`、`SIMPLE_AR_LLM_THINKING` | 旧推理控制 | 字符串，默认空；选项同 profile | 下次创建兼容连接 |
| `SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS`、`SIMPLE_AR_LLM_STREAM` | 推理上限／流式 | 可选正整数；布尔文本，默认 false | 下次创建兼容连接 |
| `SIMPLE_AR_INPUT_PRICE_PER_1M`、`SIMPLE_AR_OUTPUT_PRICE_PER_1M` | 旧费用估计 | 可选数值 | 下次创建兼容连接；不保证消费上限 |

<a id="model-file"></a>

<a id="命名模型连接"></a>

## `models.toml`：模型连接与路由

位置选择顺序为：入口支持的显式路径 → `SIMPLE_AR_MODELS_CONFIG` → 用户目录下的 `~/.config/simple-ar/models.toml`。只加载一份文件，不合并项目配置；续接时也可使用任务保存的绝对位置。共享配置建议设置绝对路径。

保存到选中的 `models.toml`，替换提供方支持的端点／模型，并私下设置 `RESEARCH_API_KEY`：

```toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://provider.example/v1"
model = "provider-model-name"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]

[routes]
default = "daily"
text = "daily"
code = "daily"
```

`simple-ar models` 检查设置与凭据是否存在，不调用 API。`models --config PATH` 只检查指定文件，不替后续命令选中它。参见[目录示例](../examples/model-profiles/models.toml)。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `version` | 目录格式 | 整数 1 | 下次加载 |
| `profiles.NAME` | 命名连接 | 至少一项的表；名称非空且不含冒号 | 新选择；检查已有身份 |
| `routes.default`、`text`、`code`、`writing`、`vision`、`image` | routes 表内的用途到 profile 映射 | 可选字符串，必须引用已有 profile | 新路由选择；保留已有绑定 |

下列字段属于 `[profiles.NAME]`。**运行设置**允许在重新加载已有连接时调整；**连接设置**在已有任务中需要显式更换连接，不能仅靠改文件切换。表中“更换连接”指这一生效方式。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `api` | 协议 | 必填 openai_chat、openai_responses、openai_images、cctq_images_async | 更换连接 |
| `base_url` | 不含用户信息、query、fragment 的端点 | 必填 HTTP(S) 字符串 | 更换连接 |
| `model` | 提供方模型 ID | 必填非空字符串 | 更换连接 |
| `api_key_env` | 凭据变量名，不是密钥值 | 必填环境变量名 | 更换连接；密钥值可轮换 |
| `capabilities` | 声明用途 | 必填非空列表：text、code、vision、image_generate、image_edit | 更换连接；不是提供方探测 |
| `proxy_env` | 保存 HTTP(S) 代理 URL 的变量名，URL 不含用户信息／query／fragment | 字符串空 | 更换连接；私下解析变量值 |
| `http2` | HTTP/2 | 布尔 false；需安装 http2 extra | 更换连接 |
| `stream` | Chat 流式 | 布尔 false；Responses 必须 false | 运行设置 |
| `request_timeout_sec` | 一次请求的超时设置，也适用于 Chat 流式响应 | 正数 180 秒 | 运行设置 |
| `connect_timeout_sec` | SDK 文本 TCP/TLS 时限 | 可选有限正数；默认连接上限 20 秒，受请求时限约束 | 运行设置 |
| `max_output_tokens` | 输出上限；省略时仍受提供方限制 | 可选正整数 | 运行设置；显式调用上限可覆盖 |
| `context_input_tokens` | 每轮送入模型的输入 token 目标 | 正整数 24000 | 运行设置；不是提供方上下文大小 |
| `retry_attempts`、`connect_retry_attempts` | 总尝试／发出前连接故障上限 | 整数 ≥1；3、1 | 运行设置；共用预算 |
| `retry_base_delay_sec`、`retry_max_delay_sec` | 重试延迟 | 正数 1、12 秒 | 运行设置 |
| `native_response_corrections` | 一次已结算原生 Chat 截断纠正 | 严格整数 0/1，默认 0 | 更换连接；需有限输出上限与共享预算 |
| `reasoning_effort` | 提供方推理强度 | 字符串空、none、minimal、low、medium、high、xhigh、max、ultra；默认空 | 更换连接 |
| `thinking_mode` | 提供方思考模式 | 字符串空、enabled、disabled、auto；默认空 | 更换连接 |
| `reasoning_output_tokens` | 活跃推理的回退输出上限 | 可选正整数 | 更换连接；显式上限优先 |
| `json_response_format` | JSON 参数策略 | auto/off/json_object；默认 auto | 更换连接；off 仍做本地校验 |
| `chat_token_limit_param` | Chat token 上限参数 | auto/max_tokens/max_completion_tokens；默认 auto | 更换连接 |
| `input_price_per_million`、`output_price_per_million` | 费用估计 | 可选非负数 | 更换连接；不是提供方消费限额 |
| `image_size` | 图像尺寸 | 可选 auto、256x256、512x512、1024x1024、1024x1536、1536x1024、1792x1024、1024x1792 | 更换连接；仅图像 API |
| `image_quality` | 图像质量 | 可选 auto、standard、hd、low、medium、high | 更换连接；仅图像 API |

字段与类型严格校验。图像 profile 只接受图像能力并拒绝文本生成设置；文本 profile 拒绝图像生成能力与尺寸／质量。Chat 禁用思考时 effort 须为空或 none。声明必须符合提供方实际支持。

| 选择器／使用方 | 解析方式 |
| --- | --- |
| env | 目录 text → default；无目录时旧环境连接 |
| profile:NAME | 完整命名连接 |
| route:NAME | 指定路由 → 能力兼容的 default |
| 有目录时的原始模型 ID | 必须匹配该用途已选连接的模型；不搜索 profile |
| 未显式指定的内嵌 CodeTask | code → default |
| 原生写作 | 显式 writing 路由，否则任务连接 |
| 视觉／图像 | 配置的兼容路由；不替换成文本客户端 |

名称、密钥或能力缺失直接失败，不切换提供方。普通任务决策与聚焦工作需要 Chat；文档写作可通过 writing 使用 Responses。视觉检查需显式 profile 输出上限。保存的身份／路由仍生效；运行调整与换密钥不会释放未知用量预留。独立 CodeTask 不对旧连接追溯固定身份。

<a id="task-file"></a>

<a id="可编辑原生任务输入"></a>

## `task.toml`：普通任务设置

<a id="引导设置"></a>
<a id="原生任务-toml-字段"></a>
<a id="v210-任务声明开发分支"></a>

优先级为默认值 → TOML → 显式 CLI 参数；CLI 中的列表替换文件列表。文件内路径相对 TOML 所在目录，CLI 路径相对运行命令的目录。

`simple-ar start --write-task-config task.toml` 导出带注释的输入，不创建任务。审阅后执行 `simple-ar start --task-config task.toml`。**使用 `--task-config` 会创建新任务；修改这份文件不会更新已经运行的任务。** 继续已有任务请用 `--resume-task`，调整额度见 [CLI 参考](CLI_REFERENCE_zh.md)。

```toml
[task]
request = "比较所提供的证据并解释其限制。"
source = ["notes.md"]
model = "env"
max_tokens = 1000000 # 示例：将本次新任务的 token 总额度设为一百万
```

将 notes.md 放在文件旁。下列均属 `[task]`，路径相对文件目录。“新任务”仅指下次据此创建，绝不追溯应用。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `request` | 目标、语言、长度与交付 | 字符串；CLI 未提供目标时必需 | 新任务 |
| `model` | 连接选择器 | 字符串 env | 新任务 |
| `source` | 只读文件／目录挂载 | 路径列表 [] | 新任务 |
| `kind` | 成果偏好 | 可选 survey、bug_fix、reproduction、writing、data_analysis、figure、code | 新任务；不固定流程 |
| `project` | 已有只读项目 | 可选路径字符串 | 新任务 |
| `document` | 原始来源 | 路径列表 [] | 新任务 |
| `material` | 笔记／草稿／结果包 | 路径列表 [] | 新任务；不是已验证原始来源 |
| `data_file`、`data_path` | 主表／额外只读数据 | 可选路径／路径列表 [] | 新任务；不推断列 |
| `output_root` | 输出父目录 | 路径 runs/assistant | 新任务位置 |
| `project_python` | 项目 Python 入口，保留 venv 入口路径 | 可选路径 | 新任务环境 |
| `allow_execution` | 允许提出进程命令 | 布尔 false | 新任务；每条命令仍须批准 |
| `allow_network` | 公共来源获取 | 布尔 false | 新任务；不授权安装 |
| `allow_vision` | 将挂载图像发送给视觉路由 | 布尔 false | 新任务；与生成分开 |
| `max_turns`、`max_tokens` | 共享模型请求／token 额度 | 整数 30、600000 | 新账本上限 |
| `max_processes` | 累计进程次数，含后端进程 | 非负整数；max_turns × 10 | 新账本；零禁用 |
| `max_process_seconds` | 累计进程墙钟时间 | 非负有限数；max_turns × 300 秒 | 新账本；不是单命令时限 |
| `max_images` | 付费生成／编辑图像提交数 | 整数 0 | 新账本 |
| `from_session` | 提供已交付材料的任务 | 可选路径 | 新工作，不是恢复 |
| `reuse` | 已登记成果组，如 artifacts | 不重复的非空字符串列表 []；与 from_session 配对 | 字节核对后复用，不继承权限／用量 |
| `extensions` | 显式可信本地包 | 路径列表 [] | 新选择；见[扩展](#可信本地扩展) |

未知字段与错误类型失败。文件只能有一个 `[task]` 表，最多 64 KiB；没有任意设置模型所选检索方法或任务策略的键。

<a id="research-file"></a>

<a id="研究-toml分区与默认值"></a>

## `research.toml`：结构化研究设置（高级）

优先级为默认值 → TOML → 显式 CLI 参数；CLI 列表替换文件列表。文件内路径相对 TOML 所在目录，命令参数保持原样，不作通用环境变量展开。

本文件分为[任务与材料](#任务材料与资源)、[检索与阅读](#检索与阅读)、[执行](#执行与报告)、[表格分析](#已有数据的描述分析)、[写作](#已有材料写作)和[历史会话续接](#研究会话补齐条件与续接)。

结构化设置使用 `simple-ar research-session --config research.toml`；不能作为额外表塞进原生 `--task-config`。

```toml
[task]
goal = "比较所提供论文的证据与限制。"
kind = "survey"
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true

[assets]
papers = ["paper.md"]
```

示例要求已配置模型且旁边存在 paper.md。未知分区、字段、错误类型失败。下表描述创建输入；已有工作见[续接](#研究会话补齐条件与续接)。

### 任务、材料与资源

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `task.goal` | 目标／交付 | 必填字符串 | 新任务 |
| `task.kind` | 结构化合同 | auto；另支持 survey、bug_fix、measurement、reproduction、writing、data_analysis | 新任务 |
| `task.outputs` | 请求成果 | 可选列表：summary、report、experiments、bug_fix、code_task、data_analysis | 新任务；不与 CLI 报告开关合用 |
| `task.output_root` | 输出父目录 | 路径 runs/research-session | 新位置 |
| `task.selected_idea_id` | 已有且有依据的候选 ID | 可选字符串 | 新选择 |
| `model.name` | 连接选择器 | TOML 默认 env；空字符串用于确定性工作 | 新任务 |
| `model.feasibility_review_model` | 同提供方可行性审阅模型 | 可选字符串 | 新任务；保留已有选择 |
| `model.max_output_tokens` | 显式模型输出上限 | 可选正整数 | 使用此输入的调用 |
| `budget.total_tokens`、`budget.llm_requests` | 共享模型额度 | 正整数；有模型的共同创建默认 600000／30；无模型时省略值为零 | 新账本 |
| `budget.process_invocations`、`budget.process_wall_seconds` | 累计执行上限 | 非负整数；省略则按任务推导 | 新账本；声明预期有限容量 |
| `assets.papers` | 本地原始来源 | 路径列表 [] | 下次读取材料 |
| `assets.materials` | 笔记／草稿／结果 | 路径列表 [] | 下次读取材料；不是原始来源 |
| `assets.data` | 实验数据输入 | 路径列表 [] | 新任务；表格分析用 analysis.file |

survey 用于文献；writing 要求本地材料且仅报告。data_analysis 可附报告，不带检索、CodeTask 或执行。measurement 要求声明命令与 experiments，可附 summary/report，不带 CodeTask。bug_fix 要求已有项目 CodeTask 与 bug_fix 输出，可附报告。显式代码交付合同可用 CodeTask 而不要求 experiments。这些结构化限制不等同于原生成果偏好。

### 检索与阅读

<a id="研究输入与行为"></a>

字段属于 `[research]`。检索片段与元数据用于发现，不是已核实原文。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `providers` | 检索提供方 | 可选列表；共同检索默认 openalex、semantic_scholar、arxiv；另支持 local_files、web | 新检索 |
| `queries` | 显式检索种子 | 字符串列表 [] | 新检索 |
| `max_results`、`max_chunks`、`idea_limit` | 检索／摄取／候选上限 | 正整数 10／300／3 | 新来源工作 |
| `max_pdf_pages` | 本地页提取上限 | 可选正整数；省略则全部页 | 新提取；冻结摄取须新任务 |
| `read_max_shortlist` | 阅读短名单上限 | 可选正整数；默认全部，最多 24 | 新阅读；显式来源过多则失败 |
| `cache_dir` | 共享全文缓存 | 可选路径 | 新缓存选择；不是安全恢复覆盖项 |
| `use_fulltext`、`allow_pdf_download`、`keep_raw_pdf` | 全文使用／远程 PDF 权限／原件保留 | 布尔 false | 新来源工作 |
| `materials_only` | 禁止在线检索 | 布尔 false；写作始终本地 | 新任务；仍可模型阅读 |
| `max_fulltext_documents`、`max_pdf_mb` | 远程篇数／单 PDF MiB 上限 | 可选正整数；省略无显式上限；引导全文设置为 6／20 | 新获取 |
| `web_extract_backend` | 提取服务 | direct / tavily_basic；默认 direct | 新提取；Tavily 需密钥 |
| `max_iterations` | 额外收集来源上限 | 非负整数 1 | 新任务；旧任务保留策略 |
| `interaction` | 确认策略 | checkpoints / assisted / autonomous；默认 checkpoints | 新任务；已有变化走续接 |

### 执行与报告

字段属于 `[execution]`。纯文献工作无需命令／CodeTask。命令是字面 argv，不经 shell 解释；权限、逐命令批准与容量分别要求。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `command` | 测量命令 | 可选非空字符串列表 | 新声明 |
| `cwd` | 已有执行目录 | 路径；CLI 默认 .；文件值相对 TOML | 新声明；argv 保持字面值 |
| `timeout_sec` | 单命令时限 | 可选正整数秒 | 新声明 |
| `code_task_config` | CodeTask 准备配置 | 可选 TOML 路径 | 新准备；除适配器外通常与 command 互斥 |
| `primary_metric`、`metrics` | 主／必需指标 | 可选字符串／列表 [] | 新 schema |
| `metric_directions` | 指标方向 | 字符串列表，如 ["accuracy=higher"]；higher/lower/resource/ignore | 新 schema |
| `baseline_policy` | 对照处理 | run / skip / reuse；配对或显式 baseline 推导 run，否则 skip | 新合同 |
| `baseline_ref` | 同条件且已通过的结果文件 | 可选路径或有序路径列表，相对输入基目录 | 新核实复用 |
| `baseline` | 对照命令／覆盖项 | 可选表；未给对照命令则用主命令 | 新比较 |
| `pairs` | 显式对照／候选条件 | 表数组；唯一整数 seed 与 argv baseline_command、candidate_command | 新比较；与单 command 互斥 |
| `seeds`、`seed_count`、`seed_flag` | 紧凑展开命令 | 可选不重复整数列表或正整数数量，加显式字符串标志 | 新条件；不与 pairs 混用 |
| `protocol` | 科学条件 | 可选表：contract_id、hypothesis、dataset_refs、split_spec、metric_specs、comparison_conditions、protected_assets；复现另需 dataset、expected_outcome | 新协议 |
| `initial_files` | 显式 CodeTask 必需创建的缺失 Python 文件 | 非空精确路径列表 | 新范围内创建，不是初始化桩 |
| `output_files` | 输出名到相对文件 | 字符串映射 {} | 新登记 |
| `metric_sources` | 指标到 JSON/CSV 选择器 | 表 {} | 新指标提取 |
| `environment` | 准备／检查设置 | 可选表；普通当前环境执行可省略 | 新准备 |

保护资产须有唯一 asset_id 与文件 path，相对执行 cwd 或绝对。复用检查命令、数据、划分、schema、条件、准备与受保护字节。种子元数据本身不推断命令或授权额外运行。

固定协议复现要求 kind reproduction、experiments（可附 report）、本地论文、materials_only=true、一个命令、有限时限、baseline_policy=skip，以及 protocol 的 hypothesis/dataset/expected_outcome。可选 CodeTask 适配器须有独立通过的基准，解析后的 argv 与测量不同。初始文件须不存在且明确允许。见[示例](../examples/conformal_reproduction/README.md)。

`[execution.environment]` 字段：

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `mode` | 任务 venv／当前环境检查 | 必填 venv / current；current 需 check_command | 新准备 |
| `requirements` | 项目相对 requirements 文件 | 字符串列表 []；不得越出执行项目 | 新 venv 安装 |
| `install_project` | 安装已声明项目包 | 布尔 false；仅 venv | 经授权的新安装 |
| `python_executable` | venv 基础解释器 | 字符串，默认运行时解释器；current 禁用 | 新 venv |
| `timeout_sec` | 每步时限 | 正整数 300 秒 | 新准备 |
| `check_command` | 简短独立检查 | 可选 argv；current 必需 | 新检查，与测量不同 |

准备共用进程容量。venv 测量要求开头是裸 Python，不静默替换显式程序。托管安装的 CodeTask 适配器须使用 current CodeTask Python、不复用源 venv，并使用自己的验证器而不是 check_command。

程序在 `SIMPLE_AR_OUTPUT_DIR` 写 runtime.json 时：

```toml
[execution.output_files]
runtime = "runtime.json"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
```

JSON 使用精确键／索引路径；CSV/TSV 使用 output、column 与可选 match（最多四个精确标量条件）。必须选中一个有限数值。登记文件为输出目录内相对 POSIX 路径的 UTF-8 普通文件，每个最多 2 MiB。字面 `{output_dir}` argv 绑定该目录。文件指标不能挽救失败进程或与 stdout 指标冲突。


<a id="已有数据的描述分析"></a>

### 已有数据的描述分析

使用 `[analysis]`、kind data_analysis、outputs data_analysis，可附 report。

```toml
[task]
goal = "描述所提供的测量。"
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
observation_unit = "一次运行"
```

仅做这类确定性分析不调用模型；生成报告需要模型资源。任务保存数据副本与设置，修改原始表不会更新已经生成的分析。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `file` | UTF-8 CSV/TSV/JSON 记录 | 必填路径 | 下次读取材料 |
| `value_columns` | 不重复数值列 | 必填非空字符串列表 | 新分析 |
| `observation_unit` | 一行的含义 | 必填非空字符串 | 新分析 |
| `group_column`、`value_unit` | 分组／共同数值单位 | 字符串空 | 新分析；省略单位仍为未知 |
| `mode` | 聚合观测／保留值 | observations / values；默认 observations | 新分析 |
| `missing` | 缺失处理 | reject / omit；默认 reject | 新分析；不补零 |
| `plot` | 图形类型 | bar / box / heatmap / line / scatter；默认 bar | 新分析 |
| `width` | 输出宽度 | wide / column；默认 wide | 新图形 |
| `max_mb`、`max_figures`、`max_points` | 输入 MiB／SVG 页／点或格上限 | 正整数 20／100／10000 | 下次读取材料／绘图；超限失败，不抽样 |
| `x_column`、`x_unit` | 数值坐标／单位 | 字符串空 | 新 line/scatter |
| `series_layout` | 坐标轴 | separate / shared；默认 separate | 新 line/scatter 图 |
| `paired_baseline` | 同行配对基线值列 | 字符串空 | 新 observations/bar 或 box 差值 |
| `association` | 描述关联 | none / pearson；默认 none | 新 line/scatter 分析 |
| `attribution` | 用户来源声明 | 字符串空 | 新分析；不验证书目信息 |

选择图形时，注意以下数据要求：

| 图形 | 数据要求与展示方式 |
| --- | --- |
| 柱图 | `observations` 汇总数量、均值、样本标准差及分位数；`values` 保留数值，要求标签唯一。 |
| 箱图 | 仅支持 `observations`；显示 Q1–Q3、中位数和实际最小／最大值。 |
| 热图 | 使用 `values`，行标签唯一且数值量纲、单位一致；不自动归一化或聚类。 |
| 折线图 | 使用 `values` 和数值 x，组内 x 唯一，按 x 排序；缺失 y 处断线。 |
| 散点图 | 使用 `values` 和数值 x，保留重复点。与折线图一样，共享坐标轴需共同单位。 |

配对差值仅用完整同行对，计算候选减基线；边际箱图可用更多行。标准误不是置信／显著性结论。Pearson 使用共同存在的 x/y；常量轴或不足两对时无定义。见[曲线示例](../examples/data-curves/README.md)。

<a id="已有材料写作"></a>

### 已有材料写作

<a id="写作约束"></a>
<a id="已有材料写作的章节规划"></a>

结构化写作使用 kind writing、仅 report、模型与本地 assets.papers/materials。路径不能同时占两种角色。支持文本、Markdown、HTML、PDF；写作材料还接受 JSON 与已校验分析包。table_analysis.v1 须与复制数据相邻，两者各限 20 MiB，导入重新检查。普通 JSON 是未核实材料。体裁／长度／预期图形写在目标中。

字段属于研究 `[report]`，不是原生 `[task]`。新工作通常选择 article，显式章节／adaptive 控制可选择 sections。显式但不兼容的 article 设置失败。引导设置可提供显式值。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `writing_method` | 整稿／章节 | 可选 article / sections；通常 article，除非控制要求 sections | 新写作；保留已存方法 |
| `template` | 内置／自定义 Markdown 提纲 | 字符串 auto；source_review、survey、material_report、experiment、reproduction、analysis_report | 新计划 |
| `reviewer` | 独立审阅 | llm / disabled；未要求 article 审阅时 disabled；sections 研究 CLI 默认 llm | 新写作／审阅 |
| `max_review_iterations` | 纠正轮数 | 非负整数；未要求 article 审阅时 0；sections 研究 CLI 默认 1 | 新审阅；零可只检查不纠正 |
| `document_review` | 整篇检查 | 布尔 false | 新审阅 |
| `review_scope` | 审阅粒度 | section / document；默认 section；document 需 document_review | 新审阅 |
| `draft_scope` | 起草粒度 | section / document / auto；默认 section，article 纠正可选 document | 新写作；document/auto 需整篇审阅／scope |
| `outline_strategy` | 提纲策略 | auto / template / adaptive；默认 auto；adaptive 需 sections | 新计划 |
| `data_tables` | 链接证据／另在正文放表 | linked / full；默认 linked | 新组装 |
| `max_section_tokens` | 章节／联合／整稿输出上限 | 非负整数 0，无额外上限 | 新模型调用 |
| `max_cited_sources` | 正文引用不同来源上限 | 非负整数 0，不设上限 | 新起草／组装；不缩证据池 |
| `max_document_review_prompt_chars` | 完整审阅请求上限 | 非负整数 0，无额外上限 | 新审阅；超限拒绝，不截断 |
| `figures` | 确定性图形设置 | 下表 | 新组装 |

article 独立检查须显式 reviewer=llm、document_review=true、review_scope=document。零轮只检查一次，正数限制整稿纠正。sections 的 draft_scope=auto 仅在所有章节有正长度目标且总计不超过 2,000 词时联合起草，否则按章节。

`[report.figures]` 字段：

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `enabled` | 支持的确定性图形 | 布尔 true | 新组装 |
| `mode` | 自动／关闭 | auto / off；默认 auto | 新组装 |
| `max_figures` | 含导入图的总上限 | 非负整数 0 为自动；正上限超出则失败 | 新组装 |
| `format` | 图形格式 | 字符串，仅 svg | 新组装 |

这些不授权付费图像生成；原生生成使用 image 路由与 max_images。写作审阅和算术检查不认证科学正确性。

<a id="研究会话补齐条件与续接"></a>

### 研究会话补齐条件与续接

<a id="准备执行后的续接"></a>
<a id="显式续接额度"></a>

新的共同任务使用 `simple-ar start --resume-task SESSION`。资源使用 `--authorize-remaining` 加授权 ID／原因；缺失或替换输入用 `--supply-task`；命名文本连接变化用 `--switch-model` 加切换 ID／原因。见 [CLI 参考](CLI_REFERENCE_zh.md)。编辑创建 TOML、默认值或已存 JSON 不能替代这些入口。

已存 ResearchApplication 会话使用 `research-session --config PATH --session-root SESSION`。该兼容入口的 `[continuation]` 接受：

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `authorization_id`、`reason` | 稳定授权 ID／说明 | 修改额度时必填字符串 | 显式续接 |
| `additional_attempts`、`additional_no_progress` | 增加尝试／无进展容量 | 可选整数 | 续接，保留用量 |
| `remaining` | 资源名到未来容量 | 非空数值映射，如 { total_tokens = 50000, llm_requests = 8 } | 续接，不重置历史 |
| `decision_id` | 精确待决事项 | 可选字符串 | 显式回答 |
| `decision_response` | 动作 | accept / reject / revise | 显式回答 |
| `decision_guidance` | 修订事实／方向 | 可选字符串 | 显式回答，不批准进程 |

重复相同授权 ID／条款用于完成中断授权，不补回已花容量；变化须新 ID。增加预算不清除费用／未知预留，也不会自行重启已完成任务。已存会话的目标／来源／执行变化可使依赖计划失效；不兼容 kind／冻结摄取须新任务。仅报告变化不重跑测量。

<a id="code-file"></a>

<a id="独立-code-task-config"></a>

## `code-task.toml`：独立代码任务（高级）

```toml
[code_task]
kind = "existing_project"
code_root = "{config_dir}/project"
task_file = "{config_dir}/task.md"

[workspace]
mode = "copy"

[edit_scope]
allowed_patterns = ["src/**"]

[execute]
to_step = "probe"
use_llm = false
```

存在相邻 project 目录／task.md 时，执行 `simple-ar code-task init --config PATH`，再执行 `simple-ar code-task execute RUN_DIR --config PATH`。测量前添加真实 benchmark.command。新项目用 kind=greenfield，除提供脚手架外省略 code_root。


<a id="加载规则"></a>

### 加载规则

init 读取项目／工作区；execute 读取执行／模型／预算。研究经 execution.code_task_config 使用同一解析器。普通相对路径使用**命令 cwd**，不是 TOML 目录。`{config_dir}` 展开 code_root/task_file/output_root、Python 路径、benchmark.command、execute.baseline_metrics_file 和 environment.required_paths。带空格命令路径须加引号。`${...}` 资源表达式被拒绝。

多数分区忽略未知键，workspace/execute.ablation 拒绝额外键；拼写错误可能无效。implementation.max_repair_attempts 不被读取，应使用 execute.repair_rounds。编辑初始化输入不重建已有工作区，新上限不重新生成已接受计划。

<a id="codetask-toml-字段参考"></a>

### CodeTask TOML 字段参考

“初始化”指新独立工作区或研究准备；“执行”指下次支持的 execute。内嵌研究保留已接受设置，使用任务续接。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `code_task.kind` | 项目类别 | existing_project / greenfield；默认前者 | 初始化 |
| `code_task.code_root` | 源码／脚手架 | 路径；已有项目必填 | 初始化 |
| `code_task.task_file` | 任务描述 | 路径；独立使用必填 | 初始化 |
| `code_task.output_root`、`code_task.name` | 父目录／标签 | 路径 runs；可选字符串 | 初始化 |
| `code_task.max_file_bytes`、`safety.max_file_bytes` | 复制文件上限，code_task 优先 | 整数 2000000；0 禁用 | 初始化 |
| `benchmark.command`、`benchmark.timeout` | 工作区命令／时限 | 可选字符串／整数秒 | 初始化／执行时限回退 |
| `benchmark.primary_metric`、`benchmark.metric_directions` | 目标／方向 | 可选字符串／字符串映射 | 初始化 |
| `metrics.primary`、`primary_metric`、`directions`、`metric_directions` | metrics 下兼容别名 | 可选字符串／映射 | 初始化；benchmark 主指标优先，后续映射／CLI 覆盖 |
| `environment.mode` | 解释器策略 | current / external；默认 current | 初始化／显式执行选择 |
| `environment.python`、`python_executable` | environment 下外部解释器，python 优先 | 可选路径；external 必需 | 初始化／显式执行选择 |
| `environment.required_paths` | 仅存在性检查 | 路径列表 [] | 每次加载 |
| `workspace.mode` | 工作区构建 | 已有项目 auto，新项目 empty；见下表 | 初始化 |
| `workspace.include`、`exclude` | workspace 下 glob | 可选字符串列表 | 初始化 |
| `workspace.reuse_source_venv` | 使用检测的源 venv | 布尔 false | 初始化；与托管任务 venv 不兼容 |
| `edit_scope.allowed_patterns` | 允许路径 | 列表 []；独立任务为空时允许其他未保护路径 | 初始化 |
| `edit_scope.protected_patterns` | 追加只读路径 | 列表 []；保留强制保护 | 初始化 |
| `edit_scope.mode` | 范围标签 | 可选字符串 | 初始化；不授予访问 |
| `safety.validation_max_file_bytes` | 静态扫描上限 | 正整数 500000 字节 | 执行回退 |
| `resource.max_runtime_sec`、`max_files`、`max_generated_lines` | resource 下资源声明／回退 | 可选正整数 | 规划／执行 |
| `resource.max_memory_mb`、`allow_gpu` | resource 下规划／交接声明 | 可选整数／布尔 | 规划；非 OS 强制限制 |
| `implementation.provider` | 后端 | local；另有 fake、local_llm、codex、claude_code、opencode、external_cli | 执行 |
| `implementation.agent_mode` | 模型／外部交接 | 可选 model / handoff；delegated_workspace 不可用 | 执行 |
| `implementation.allow_external_agent` | 外部启动授权 | 布尔 false | 执行 |
| `implementation.agent_model`、`agent_binary`、`agent_args` | implementation 下外部模型／程序／argv | 可选字符串／列表；空模型用外部账号默认值 | 执行 |
| `implementation.agent_timeout_sec` | 外部时限 | 正整数 600 秒 | 执行 |

<a id="execute-与-budget-字段"></a>

### Execute 与 Budget 字段

<a id="execute-与-budget"></a>

CodeTask 预算与研究预算分开，显式 execute CLI 优先。解释器选择不安装依赖，只解析开头裸 Python，保留其余 argv。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `execute.to_step` | 停止点 | 字符串 run；支持步骤见 CLI | 下次执行 |
| `execute.use_llm`、`llm.enabled` | 模型使用，execute 优先 | 布尔 true | 下次执行 |
| `execute.model` | 通用选择器 | 可选字符串；回退 models.code_task.default → models.default → llm.model | 下次执行 |
| `models.code_task.default`、`models.default`、`llm.model` | 通用回退选择器 | 可选字符串 | 下次执行 |
| `models.code_task.planner`、`writer`、`reviewer`、`editor`、`repair`、`summarizer` | models.code_task 下角色选择器 | 可选字符串；回退通用连接 | 下次执行 |
| `execute.timeout_sec`、`execute.timeout` | 单命令时限 | 正整数；timeout_sec → timeout → benchmark.timeout → resource.max_runtime_sec → 60 秒 | 下次执行 |
| `execute.skip_validation`、`strict_validation` | execute 下验证控制 | 布尔 false | 下次执行 |
| `execute.validation_max_file_bytes` | 静态上限 | 正整数；safety 回退，再 500000 | 下次执行 |
| `execute.stream_benchmark_output` | 终端输出 | 字符串／布尔 off/false；另有 line、auto/true、summary | 下次执行 |
| `execute.baseline_policy` | 基线处理 | auto；另有 run、skip、none、provided | 下次执行；provided 不复测 |
| `execute.baseline_metrics_file` | 已供数值 JSON | 可选路径；平映射或 metrics/metric_values 包装 | 下次执行 |
| `execute.apply_proposed_edits`、`allow_large_edits`、`allow_planning_fallback` | execute 下显式选择 | 布尔 false | 下次执行；保留正常检查 |
| `execute.planning_mode`、`planning_review_rounds` | execute 下规划／兼容项 | compact（tool_agent 弃用别名）；整数 0 | 新规划；旧审阅设置不授予调用 |
| `execute.llm_retry_attempts`、`repair_rounds` | execute 下响应纠正／技术修复 | 正整数 3／非负整数 0 | 下次执行；与传输重试分开 |
| `execute.max_files` | 规划文件上限 | 正整数；resource.max_files → budget.max_files → 8 | 新规划 |
| `execute.max_source_chars_per_file` | 源码上下文上限 | 正整数 4000 | 下次规划 |
| `execute.max_generated_lines` | 生成行数上限 | 正整数；resource 回退，再 1600 | 新规划 |
| `execute.ablation.review_gate` | 审阅阻塞等级 | strict / runtime；默认 strict | 下次执行；runtime 仍阻塞安全／可执行缺陷 |
| `execute.budget_profile`、`budget.profile` | 提案档位，execute 优先 | normal / large / absolute；默认 normal | 下次提案；较大档位需批准 |
| `execute.max_batches`、`budget.max_batches` | 批次上限，execute 优先 | 可选正整数 | 下次执行 |
| `execute.cost_cap_usd`、`budget.cost_cap_usd` | 估计费用上限，execute 优先 | 可选非负数 | 下次执行；需价格 |
| `budget.max_files`、`max_edits`、`max_old_chars`、`max_new_chars`、`max_total_edit_chars`、`max_proposal_chars` | budget 或 budget.PROFILE 下提案上限 | 正整数；默认如下 | 下次提案；档位表覆盖顶层 |

| 档位 | 文件 | 编辑 | 旧块字符 | 新块字符 | 总编辑字符 | 提案字符 |
| --- | --- | --- | --- | --- | --- | --- |
| normal | 4 | 4 | 3000 | 4000 | 12000 | 24000 |
| large | 4 | 8 | 12000 | 16000 | 48000 | 96000 |
| absolute | 8 | 16 | 24000 | 32000 | 120000 | 240000 |

模型 token、进程、编辑大小、纠正轮数与费用估计是独立限制。修复／外部候选仍须范围、审阅与基准验证。

<a id="工作区模式"></a>

### 工作区模式

| 模式 | 初始化效果 |
| --- | --- |
| auto | 已有项目默认；可行时 detached Git worktree，否则受保护复制；研究复制脏源码。 |
| copy | 符合条件的当前文件，含未提交变化，受排除／大小限制。 |
| git_worktree | 已提交本地 HEAD；需至少一次提交；显式失败不降级。 |
| sparse_copy | include/exclude 加强制排除；可能遗漏依赖。 |
| empty | 新项目默认；copy/sparse 脚手架需 code_root；workspace CLI 不接受 empty。 |

<a id="编辑范围规则"></a>

### 编辑范围规则

<a id="edit-scope-behavior"></a>

编辑发生在 CodeTask 工作区内。非空允许列表缩小路径；保护优先。保留内置测试／benchmark／秘密／凭据保护。研究准备未提供显式允许列表时保留默认源码范围；用 glob 明确缩小。工作区隔离不是 OS 沙箱。

<a id="在研究会话中使用-codetask"></a>

### 在研究会话中使用 CodeTask

引用 execution.code_task_config，在研究权限与共享进程容量下完成范围内准备、编辑、验证；需要时另行声明测量。已接受范围／环境／预算持久保留，编辑引用的 TOML 不会静默替换。

<a id="extension-file"></a>

<a id="可信本地扩展"></a>

## `extension.toml`：本地扩展（高级）

只加载显式选择的包，不自动发现／下载／安装。原生 task 的 extensions 路径指定含 extension.toml 的目录或描述文件，相对任务 TOML。

```toml
[task]
request = "总结所提供的笔记。"
source = ["notes.md"]
extensions = ["../my-skill"]
```

下列字段属于包的 extension.toml。包可只提供说明、工具或两者。变化需新选择；续接要求描述文件、代码、说明与声明资源的原路径／哈希。

| 字段 | 含义 | 类型与默认值 | 修改后何时生效 |
| --- | --- | --- | --- |
| `extension.id`、`version`、`description`、`scope` | extension 下元数据 | 必填非空字符串；ID 匹配 [a-z][a-z0-9_]{0,23} | 新选择 |
| `extension.instructions`、`resources` | extension 下包内文件 | 可选路径／路径列表 [] | 新选择 |
| `tools` | 工具声明 | 可选表数组 | 新选择 |
| `tools.name`、`description` | 工具元数据 | 必填字符串；名称遵循 ID 格式，instructions 保留 | 新选择 |
| `tools.implementation`、`function` | 独立 .py 与函数标识符 | 工具必填字符串 | 新选择 |
| `tools.permission_level` | 声明操作类别 | read_only / plan | 新选择；作者保证无副作用 |
| `tools.input_schema` | 对象参数 schema | 必填 Draft 2020-12 表；仅本地引用 | 新选择 |
| `tools.completion` | 可选交付／问题完成 | 表；kind delivery、field 空、closing false | 新选择 |

公开名称为 `id__name`，说明通过 `id__instructions` 加载。可选完成声明用 `{ kind = "delivery", field = "handoff", closing = false }`：指定 schema 字段含 summary 及可选 artifacts/unresolved；field 为空则整个对象为交付。kind=question 改用 `{question = "..."}`。完成声明不增加权限或预算。

扩展 Python 即使声明 read_only/plan，仍拥有宿主 OS 权限。宿主服务检查已授权挂载操作，不能沙箱化 Python 自己的文件／进程／网络访问。未声明依赖不在身份检查范围。见完整[描述文件](../examples/local_extension/extension.toml)与[说明](../examples/local_extension/SKILL.md)。

<a id="saved-task"></a>

## 已保存任务目录：查看与调整

任务目录中的状态、用量账本和恢复记录由系统维护。修改创建任务时的 TOML 不会更新这些记录，也不要直接编辑内部 JSON。

- 继续任务或提出反馈：使用 `simple-ar start --resume-task SESSION`。
- 查看当前交付：使用 `simple-ar results SESSION`；查看状态和用量：使用 `simple-ar status SESSION`。
- 调整剩余额度、补齐配置或更换连接：使用 [CLI 的续接参数](CLI_REFERENCE_zh.md#simple-ar-start)。这些操作保留已经发生的用量和失败记录。
