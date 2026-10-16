# 配置参考

[English version](CONFIG_REFERENCE.md) · [使用指南](USAGE_zh.md) · [CLI 参考](CLI_REFERENCE_zh.md)

本文用于查字段、默认值和约束。模型配置文件保存命名连接，研究 TOML 定义精确任务，CodeTask TOML 定义可编辑项目。密钥保存在环境变量或私有 `.env`。

| 查阅内容 | 章节 |
| --- | --- |
| 连接选择、profile 字段和传输 | [命名模型连接](#命名模型连接) |
| 凭据与部署变量 | [凭据与环境变量](#凭据与环境变量) |
| 目标、材料、资源额度和执行 | [研究 TOML](#研究-toml分区与默认值) |
| 写作方法与报告设置 | [已有材料写作](#已有材料写作) |
| 表格字段与统计变换 | [已有数据](#已有数据的描述分析) |
| 决定与追加额度 | [续接](#研究会话补齐条件与续接) |
| 项目、解释器、模型和编辑预算 | [CodeTask 字段](#codetask-toml-字段参考) |

## 引导设置

普通 `start`、`--guided` 和成果偏好保存原生会话，不生成可编辑研究 TOML。启动参数提供目标、只读材料、权限、解释器和额度，恢复沿用保存值。显式结构化协议和 `--chat` 保存研究 TOML，必要时引用 CodeTask TOML。设置草稿不是执行会话。

结构化优先级为默认值 → TOML → 明确 CLI，CLI 列表替换文件列表。研究路径相对 TOML 所在目录解析，命令 argv 保持字面含义；协议保护路径相对执行 cwd。CodeTask 使用独立的[加载规则](#加载规则)。原生追加访问或资源须走明确续接参数。

### Python 创建任务

结构化调研、材料写作和表格输入可与 CLI 共用创建所有者：

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

创建只校验输入，不调用模型或进程。不传 `model` 时由调用方提供动作，后续可明确补齐条件；声明报告只是保留交付义务，不授权调用模型。`ResearchApplicationServices` 提供研究设置及有限预算。不支持的旧执行或修订控制在创建前拒绝，不静默丢弃；旧应用 API 仍承接其已有合同及历史继续。

可信 Python 集成可通过 services 注入文本客户端、独立审阅客户端和 `SearchProviderRegistry`。客户端须实现 `with_budget` 及 `ask_json` 或 `chat_turn`，绑定任务实际账本、会话与 attempt。继续服务工作前，用 `ToolApplication.load(path, services=services)` 恢复依赖。持久化只保留类型/模型身份和 provider 名称，不保存密钥或可执行对象；缺失依赖不会回退环境客户端或内置检索器。自定义实现由调用方信任，不是安全沙箱，也不独立验证实现身份。需跨 CLI/重启使用时，优先命名连接；`model="env"` 表示配置的默认模型，不是名为 env 的服务商模型。

`snapshot_sources=True` 只复制明确选中的普通文件，每个受 `max_snapshot_bytes` 限制（默认 16 MiB），不复制整个目录；哈希绑定保留字节。旧 v1 导入共用这些副本，保留父任务关系与原文件，历史预算标为 `unknown_not_imported`，不导入或清零。新任务沿 `start --resume-task` 继续，原任务仍按原合同恢复。

### 命名模型连接

一次读取一份模型 TOML，不合并多份文件。支持配置路径参数的命令可显式指定文件；否则使用 `SIMPLE_AR_MODELS_CONFIG`，再查找 `~/.config/simple-ar/models.toml`。`models --config PATH` 只检查该文件，不会替后续命令启用它。

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

| 选择器／使用方 | 解析规则 |
| --- | --- |
| `env` | 目录 text，再 default；无目录则用旧环境连接 |
| `profile:NAME` | 完整命名连接 |
| `route:NAME` | 用途路由，再使用能力匹配的 default |
| 嵌套 CodeTask | code，再 default，除非明确选择 |
| 原生写作 | 明确 writing 路由，否则保持任务所选连接，包括明确 profile |
| 视觉／图像 | 已配置且能力匹配的 vision/image 连接，不用文本客户端替代 |

`[model].name` 也接受选择器。裸模型覆盖不能让命名连接换模型，应另建 profile。名称、密钥或能力缺失时失败，不切换提供方。profile 名称须非空且不含冒号。路由为 default、text、code、writing、vision、image。

| Profile 字段 | 默认值／取值 |
| --- | --- |
| `api` | 必填：openai_chat、openai_responses、openai_images、cctq_images_async |
| `base_url`、`model`、`api_key_env`、`capabilities` | 必填：无用户信息/query/fragment 的 HTTP(S) URL；非空模型；密钥变量名；text/code/vision/image_generate/image_edit 中的非空能力列表 |
| `proxy_env` | 默认空，引用保存 HTTP(S) 代理 URL 的变量；不接受用户信息/query/fragment，实际地址不持久化 |
| `stream`、`http2` | false、false；仅 Chat 支持流式 |
| `request_timeout_sec` | 180，正数秒 |
| `max_output_tokens` | 未设，显式正整数输出上限 |
| `context_input_tokens` | 24000，正整数原生输入工作集目标，不代表提供方上下文容量 |
| `retry_attempts` | 3，至少 1，包含首次物理请求 |
| `connect_retry_attempts` | 默认 1；仅对能确认尚未发送请求的连接故障提供更高尝试上限。每次消耗请求预算，不扩大发送后中断或用量未知响应的重试范围 |
| `retry_base_delay_sec`、`retry_max_delay_sec` | 1、12，正数秒 |
| `reasoning_effort` | 独立于精确 `model` 名称的可选推理参数；空表示不发送。具体支持取决于服务商及模型，与配置别名或Codex subagent设置不是一回事。可配置none、minimal、low、medium、high、xhigh、max、ultra；请求某档位不代表已证明服务商实际执行了它。 |
| `thinking_mode` | 空、enabled、disabled、auto；Chat 关闭思考时 effort 须为空或 none |
| `reasoning_output_tokens` | 未设，启用 effort 时的正整数备用上限；显式调用/profile 上限优先 |
| `json_response_format` | auto、off、json_object；off 不发原生 JSON 参数，仍本地校验 |
| `chat_token_limit_param` | auto、max_tokens、max_completion_tokens |
| `input_price_per_million`、`output_price_per_million` | 未设，非负费用估计 |
| `image_size` | 未设；auto、256x256、512x512、1024x1024、1024x1536、1536x1024、1792x1024、1024x1792 |
| `image_quality` | 未设；auto、standard、hd、low、medium、high |

目录严格检查字段与类型。Images 只接受图像能力，拒绝明确提供的文本流式/token/context/推理/JSON/单价字段。文本连接拒绝图像尺寸、质量和生成能力。能力是声明，不是 API 探测；提供方参数支持需要核实。原生决策和 `focus_work` 要求 Chat，`compose_document` 可通过写作路由使用 Responses。焦点切换不改变工具或共享额度。

新命名研究/原生任务与设置草稿保存非秘密绑定，固定模型、端点、协议、HTTP/2、推理设置及相关代码/写作路由。超时、输出上限、重试/等待和输入工作集可调整；同一变量背后的密钥可轮换。视觉授权单独绑定；无关图像 profile 不使文本任务失效。无绑定旧任务保留旧解析，独立 CodeTask 不追溯补绑定。失败原生决策换连接须走 CLI 明确重试合同。

HTTP/2 需要执行主机安装 `simple-autoresearch[http2]`，源码用 `uv sync --extra http2`。图像缺支持时在预留/提交前失败，完整旧图片仍可离线恢复。profile 代理只影响本连接，缺失或无效在付费前失败；未配置时沿标准客户端环境路由。

有限时限的 SDK Chat 流式同时约束提交和消费，之后最多两秒清理。建连最多 `min(20, timeout)` 秒，读取/写入/连接池保持配置时限。SDK 内部重试关闭。缺完成标记保留未知用量；有输出上限的文本可沿同 API 在次数内重试，保留失败预留。无上限/视觉请求及额度不足时停止。非流式、Responses、LiteLLM 保持各自运输时限。已有 asyncio 循环中使用 `await asyncio.to_thread(client.ask, ...)`。

推理档位和上限依提供方而异，推理可能同时占输出和后续输入。达到长度上限且只有思考、无最终回答不算完成；延长时限不能解决立即过载。诊断保留异常类型、可取得的 HTTP 状态与白名单错误码/类型；SDK Chat 失败还保留已观察到的最后一个安全 TCP/TLS/HTTP 事件，不记录请求头或 trace 内容。发送前建连失败不同于响应中断后的未知用量，之后一次 TLS 成功也不证明 API 稳定。不设输出上限时最终回答可能超过按输入估计的剩余额度，任务额度不是提供方严格费用上限。

<a id="全局-env"></a>

## 凭据与环境变量

凭据与部署设置放环境变量或此文件，任务路径/数据/解释器放任务输入或 TOML。目录 profile 优先于旧模型/URL/密钥设置。

| 变量 | 用途／旧默认值 |
| --- | --- |
| `SIMPLE_AR_MODELS_CONFIG` | 模型 TOML 文件路径，共享设置建议使用绝对路径 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`SIMPLE_AR_MODEL` | 旧连接，模型默认 gpt-4o-mini |
| `SIMPLE_AR_LLM_TIMEOUT_SEC`、`SIMPLE_AR_MAX_OUTPUT_TOKENS` | 180 秒，输出上限未设 |
| `SIMPLE_AR_LLM_RETRY_ATTEMPTS`、`SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC`、`SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC` | 3 次，1/12 秒等待 |
| `SIMPLE_AR_LLM_BACKEND`、`SIMPLE_AR_LLM_API` | 旧运输/API |
| `SIMPLE_AR_JSON_RESPONSE_FORMAT`、`SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` | 旧 JSON/token 参数政策 |
| `SIMPLE_AR_LLM_REASONING_EFFORT`、`SIMPLE_AR_LLM_THINKING`、`SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS`、`SIMPLE_AR_LLM_STREAM` | 旧推理/流式控制 |
| `SIMPLE_AR_INPUT_PRICE_PER_1M`、`SIMPLE_AR_OUTPUT_PRICE_PER_1M` | 可选费用估计 |
| `SIMPLE_AR_LLM_MAX_WORKERS` | 共享模型 worker 上限 |
| `OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY` | 独立可选来源凭据，匿名访问仍受额度限制 |
| `TAVILY_API_KEY` | 可选 web 基础搜索/tavily_basic 提取必需 |
| `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS`、`SIMPLE_AR_DOCUMENT_PROXY_ENV`、`SIMPLE_AR_DOCUMENT_CA_BUNDLE` | 关联资料准确主机列表、代理变量名、可选可信 CA 文件 |
| `SIMPLE_AR_RESEARCH_INDEX_ROOT` | 显式清理历史索引的位置，默认 `.simple_ar_cache/research_index`；不控制资料检索后端。 |

关联资料路由保持 TLS 校验，拒绝主机通配符，只影响关联补充材料，不影响模型/检索/PDF/ZIP。未列主机直连；未明确配置时此下载器不使用环境代理。`SIMPLE_AR_OUTPUT_DIR` 每次进程提供，不是全局设置。

连接器凭据放认证请求头。HTTP 429 后本批次停止实时请求该提供方，仍检查已启用查询缓存并继续其他提供方。Web 只用 Tavily basic，不升级、不生成答案、不自动重试，缺凭据失败；无日期页面不满足年份条件。提取与检索分开授权和缓存，保留原 URL/文本/请求 ID/报告用量；复用不调用，失败不升级或切换后端，PDF 许可仍独立。摘要与元数据不冒充已验证原文。

## 研究 TOML：分区与默认值

~~~toml
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
~~~

使用 `simple-ar research-session --config research.toml`，见[调研示例](../examples/survey/README.md)。未知研究分区/字段/类型报错。省略 API 上限表示该维度不设框架上限，不表示免费或无限重试；恢复保留用量和限额。

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
| `budget.total_tokens`、`budget.llm_requests` | 可选正整数 API 上限 |
| `budget.process_invocations`、`budget.process_wall_seconds` | 非负，默认按任务推导；应声明有限的拟执行额度 |

以下限制适用于结构化 `research-session`，不代表普通 `start` 的工具组合范围。

| Kind | 约束 |
| --- | --- |
| survey | 只读文献，除明确代码交付兼容合同外不执行 |
| bug_fix | 已有项目 CodeTask；输出 bug_fix，可加 report；不跑基线 |
| measurement | 明确命令、experiments及可选report（须模型）、有限进程额度；固定配对可共享一次可核环境准备；不发现/设计/改码或复用历史基线 |
| reproduction | 下述固定协议，experiments 可加 report |
| writing | 本地材料/论文，仅 report，不执行 |
| data_analysis | 已有表，data_analysis 可加 report，不研究/CodeTask/进程执行 |
| auto | 已接受计划选择有界步骤，不自动寻找仓库或安装 |

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
| `research.max_iterations`、`research.interaction` | 1轮后续；0首轮分析后停。新 CLI checkpoints，可选 assisted/autonomous |
| `assets.papers`、`assets.materials`、`assets.data` | 书目来源；写作草稿/笔记/结果/包；执行输入。项目内数据复制进隔离准备，外部原地保留，不改 argv 或核划分 |

本地 PDF 不受远程全文关闭影响；远程 PDF 需要下载/保留许可。出处记录总页/提取页/空页/截断，提取不等于 OCR 或理解。基础 parser 执行页数上限；unstructured 无法执行时拒绝，旧覆盖不补造。

搜索会聚焦查询并保留用户提供的初始查询。arXiv 的普通关键词以 `AND` 连接，高级表达式保持原样。启用模型阅读与自动扩展后，可在原额度内按证据缺口补做一次搜索、获取和阅读。

关联页面获取每次最多进行 4 次 GET，每次都检查公开地址。必要材料或权限不足时，各种交互策略都不能跳过检查继续执行受影响的操作。

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
| `initial_files` | 须显式 CodeTask 准备；明确允许、尚不存在、不受保护的 Python 路径，不限复现任务。已支持的原生任务交由 CodeTask 直接创建，不先写占位实现；历史及未迁移合同仍由原所有者执行。 |

仅文献任务省略 command 和 CodeTask。

固定测量使用 `reuse` 时，`baseline_ref` 路径相对配置文件（无配置文件时相对当前目录）。框架核对命令、指标 schema、比较协议与明确保护文件的原始哈希，并把原结果保存为任务内只读快照；只执行候选条件，基线保留原测量身份、不新增进程或用量记录。缺少保护文件证据、条件不匹配、失败结果或已变化的输入会阻断复用。受控 CodeTask 也可消费同一外部基线：原观察和快照哈希绑定实施审批，直接传给代码模块，不把基线冒充为候选准备阶段的产物。若导入结果声明了产生它的准备或实施血缘，仍须由原血缘所有者处理，不能只凭一个结果文件抹去这些关系；历史结果与账本保留。

| `[execution.environment]` 字段 | 默认值／约束 |
| --- | --- |
| `mode` | venv 创建任务环境；current 必须明确检查，不安装或替换解释器；普通 current 执行省略此表 |
| `requirements`、`install_project` | []、false；requirements 相对执行 cwd |
| `python_executable` | venv 基础解释器，默认运行时 |
| `timeout_sec`、`check_command` | 每步300秒，可选字面短检 argv |

准备与检查共享进程预算/日志，失败不进入测量，检查输出不作为测量。任务 venv 解析裸 Python 别名；来源绑定测量中的显式执行器若与该 venv 冲突，会在启动前拒绝，不静默替换。安装可能运行构建代码/联网；工作区不是 OS 沙箱，完成恢复不重装。

已迁移的精确任务可先准备未改源码、测基线，再实施独立候选，反馈时复用原准备。冻结条件的来源与 baseline/candidate 比较角色分开。明确 `skip` 只要求声明的候选运行，交付描述性结果，不虚构基线对照。固定测量与 CodeTask 可按上述 `baseline_ref` 复用外部 canonical 基线；带产生来源血缘的导入及未支持的结构化组合仍由原控制器负责。

普通 benchmark 允许 validator 与正式测量使用相同 argv，但必须分别进行真实物理调用并独立记账，validator 结果不能充当测量。严格 reproduction 仍要求不同的校验命令。greenfield 的 `copy`/`sparse_copy` 若有真实 source 项目，可承接源码基线；这些冻结条件显式携带 `source="preparation"`、`before_implementation=true`。高层须先 prepare，完成全部声明的 baseline，再调用 `implement(prepared_result)` 并传入实际成功基线事实；失败或缺测不能授权生成。empty 或无 source 的 greenfield baseline 保留准确的原 owner 或拒绝，不虚构源码。项目测量 cwd 仍须匹配原 source root，可写范围与源码路径投射规则不变。

~~~toml
[execution.output_files]
runtime = "runtime.json"
observations = "tables/observations.csv"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "observations", column = "coverage", match = { method = "candidate" } }
~~~

附件必须位于本次执行的 `SIMPLE_AR_OUTPUT_DIR` 中。命令参数可以使用字面占位符 `{output_dir}`，它不会经过 shell 展开。登记时使用相对 POSIX 路径。附件数量没有上限；每个附件必须是 UTF-8 普通文件，且大小不超过 2 MiB。

登记后的附件会提供预览和读取句柄。系统不会扫描当前工作目录、从 stdout 猜测文件路径，也不会复制外部文件。附件缺失、不可读或过大时，会单独报告，不影响指标成功状态，但这些附件不能支撑结论。

`results.json.output_evidence` 记录附件的来源和截断情况。读取时可以指定字符窗口、查找字面文本，或用最多四个精确标量条件筛选 JSON 数组、CSV 和 TSV 中的记录。这些读取操作不会汇总记录。

JSON 指标选明确键/下标；CSV/TSV 必须唯一一行。缺失、歧义、非有限或与 stdout 冲突失败；文件不能使失败/超时进程变为成功测量。改变附件合同属于执行修订，不是报告刷新。

#### 固定协议复现

要求 materials_only=true、本地 papers、command、有限 timeout、baseline_policy="skip" 及 hypothesis/dataset/expected_outcome。不创新、不扩种子、不暗中寻找环境。

可选 CodeTask 准备范围内 adapter/入口；其 benchmark 为独立验证，execute timeout 为正验证时限。解析解释器后验证 argv 仍须与测量不同，先验证通过。已支持的平直任务由 CodeTask 直接创建声明的 initial files，不先造占位实现；历史合同保持原恢复行为。不授权方法/划分/条件变化。明确的 current 检查或任务 venv 在实施前运行，先检查准备加验证的完整额度；准备检查不能运行正式测量。venv 要求 CodeTask 初始使用 current Python，且不与源 venv 复用组合。反馈沿实际记录复用环境，不重复安装。见[复现案例](../examples/conformal_reproduction/README.md)。

#### 配对与种子

每个 `[[execution.pairs]]` 须提供唯一整数 `seed`，以及两个字面 argv 键 `baseline_command`、`candidate_command`：

~~~toml
[[execution.pairs]]
seed = 0
baseline_command = ["python", "evaluate.py", "--method", "baseline", "--seed", "0"]
candidate_command = ["python", "evaluate.py", "--method", "candidate", "--seed", "0"]
~~~

另一方式用 command、唯一 seeds 或正 seed_count，加明确 seed_flag。不能混用 pairs。已有 `--seed 0`/`--seed=0` 被替换而非重复追加；单 flag 只登记当前种子，不授权扩展。协议内部 seeds 仅描述评估器条件，仍一次命令。

扩展须有分析建议、同种子配对、接受决定和剩余进程额度；命令/协议种子冲突拒绝。

基线身份核对命令/schema/数据/划分/指标/条件/协议版本/准备 lineage/保护文件，不凭候选 ID、假设或 cwd。旧证据不从新计划补齐，论文值不当本地测量。保护文件须唯一 asset_id/path，路径相对 cwd 或绝对，检查仅覆盖列出的文件。为每次矩阵调用预留预算；直接 argv 用进程 PATH，CodeTask 解析裸 Python。

## 已有材料写作

结构化写作设置 `kind="writing"` 和 `outputs=["report"]`，提供材料及模型连接。普通 `start --kind writing` 则创建原生任务。

论文放入 `assets.papers`，草稿、笔记和结果说明放入 `assets.materials`；同一路径不能重复登记或同时承担两种角色。支持文本、Markdown、HTML 和 PDF。普通 JSON 只作为尚未核实的材料；无效 JSON 或损坏的 `table_analysis.*` 分析包会报错，不会降级后继续写作。

导入 `table_analysis.v1` 时，须保留同目录的数据副本；JSON 与输入数据各不超过 20 MiB。系统复算数值并重建图形，拒绝过期或缺项的包。复算检查算术，不证明数据采集或科学解释正确；原始表格也不是已验证的实验包。提取后的材料会保存供恢复使用。

在目标中说明文体、长度和计数口径。大致期望与严格范围应明确区分；系统不会自行把页数换算成词数或添加容差。标题、章节和参考文献是否计入长度，沿用已保存的要求。

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

`article` 用一次请求起草整篇正文，随后装配并执行确定性检查。设 `reviewer="llm"`、`document_review=true`、`review_scope="document"` 可独立检查；0轮不改写，正数轮次复用已有联合编辑器，保留初稿响应，提出完整候选，独立检查后才采用。被拒绝或不可用的提案仍计入轮次；保存候选及已完成检查恢复时不重复调用。未知或拒绝响应不盲目重发；必要纠正未完成则失败，不发布为成功修订。这些检查不等于科学认证。

固定结构和引用上限无需切换到分节 Writer。未开启自动纠正时，一级章节缺失、重复、多出、顺序错误或引用超量会保留原响应并拒绝采用，不重发请求。开启有界纠正后，可以保留完整初候选供编辑，但约束与独立检查都通过后才可交付。子章节自由，不兼容的显式设置报错。刷新或修订可使用已采用旧稿，与原证据分开；历史响应和费用继续保留。历史整稿按保存约束恢复，不追套当前默认值。

`sections` 保存章节计划和检查点，并进行有限纠正。`draft_scope="auto"` 仅在每节都有正数长度目标、总目标不超过 2,000 词时联合起草，否则分节处理。新的 document/auto 纠正任务直接检查当前稿，不再额外核验一遍旧意见；只有检查完整覆盖当前稿，才能替代旧意见，原意见仍保留在历史中。已保存检查恢复时不再重发该检查，历史检查点保留原核验合同。联合修订以完整候选为单位，独立检查整个候选后才采用；被拒绝或待处理的轮次仍计入额度。整稿检查无法完成时，会保存草稿并暂停。

分节方法可在授权范围内补读已登记原文，共享现有工具额度；这不授权新的搜索、任意文件读取或进程执行。补读的内部运行参数不是公开研究 TOML 字段。

Auto 模板：≤1可引用来源为 source_review，多来源 survey，供材 material_report，实验依据目标判断选 experiment/reproduction/analysis_report。明确模板不覆盖事实，执行 passed 不等于科学成功；保存解析模板沿用。非综述 adaptive 分节规划增加1调用及最多1纠正，无效指针拒绝、回退须 runtime 明确许可。图归属可按证据规划；结构化引导写作保存 adaptive，数据/调研/复现可保存联合写作审阅，不等于基础默认。

两种 data_tables 都保留完整可搬迁包。唯一图/链接归冻结章节，歧义用描述数据节。实验包保留 experiment_evidence.json、可读 companion 和登记副本，审计核投影/存在，不核科学或后续篡改。ACM 带登记包，不复制任意链接。概念图须有依据，不为数量编图；未知书目仍未知。

<a id="已有材料写作的章节规划"></a>

章节规划字段和方法约束见上表；旧链接仍指向本写作参考。

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

保存配置前检查表格结构和列名；正式分析时检查数值并保存输入快照。缺失值不会填成零，非数值和非有限数会报错。恢复使用已保存数据，改变分析变换需要新任务。

仅分析不调用模型。追加报告需要模型连接并共享会话预算，可附本地说明或论文，但不会因此改变已确认的数值设置或获得联网权限。

| 图 | 模式／行为 |
| --- | --- |
| `bar` | `observations` 计算分组计数、均值、样本标准差与四分位数；`values` 保留给定值，并要求标签唯一 |
| `box` | 仅用于 `observations`，显示四分位数、中位数和实际最小/最大值；四分位数按 `(n−1)p` 插值，不接受坐标或 Pearson 选项 |
| `heatmap` | 使用 `values`，要求唯一行标签及声明的共同量和单位。跨页共用原值色标，不归一化、排序或聚类；`omit` 保留缺失格，全缺失矩阵报错。不接受坐标、Pearson、配对或 shared 选项 |
| `line` | 使用 `values` 和数值 x；每组 x 须唯一、完整，并按 x 排序；缺失 y 时断线 |
| `scatter` | 使用 `values` 和数值 x；保留重复坐标。`omit` 会在数据包保留缺坐标行，但不绘制这些行 |

箱线图的须表示实际最小值与最大值，不是置信区间或 Tukey 界。坐标图不自动聚合或拟合；其中的分组表示系列，不声明配对或重复采样。

共用坐标轴要求声明共同单位，不会自动归一化。点数上限包含所有系列；热图上限也包含缺失单元格。分页保留类别与统一尺度，不通过丢弃标签或记录来满足限制。

配对比较要求 `mode="observations"`、`bar` 或 `box`，并同时选择基线与候选列。用户须确认同一行的匹配关系和共同单位。

计算采用“候选减基线”，保留完整与缺失对数、均值、样本标准差和标准误。`omit` 只使用双方都非缺失的行，不是分别过滤后再相减。至少两个独立配对时显示 ±1 标准误，不等于置信区间或显著性结论。箱线图的单列分布与配对差值使用不同的有效行，分别展示。

Pearson 相关只用于 `line` 或 `scatter` 的 x/y 坐标，每组、每列使用共同非缺失的坐标对。常量轴或不足两对时结果未定义；只有两对非恒定值时，相关系数必为 ±1。

输出保留样本数与未定义原因，不改动原图点，也不作回归、显著性、因果或独立性判断。接近零的相关系数不能排除非线性关系。见[曲线案例](../examples/data-curves/README.md)。

## 研究会话补齐条件与续接

使用 `research-session --config research.toml --session-root SESSION` 续接。省略的目标和输出沿用原值；显式修改目标、论文或执行条件时，只重建相关计划并保留历史。测量复用要求命令、结果格式、协议、准备条件和保护资产一致。

改变任务类型，或采用不兼容的搜索、提取设置，需要新会话。仅修改报告设置不会重跑测量；`research-report` 可为已有会话补写报告。

`continuation.decision_id` 指定待答复决定；`decision_response` 为 `accept`、`reject` 或 `revise`，修订说明放在 `decision_guidance`。修改交付要求时还需提供对应报告设置。

相同请求可恢复中断的修订，不重复已完成工作；改变输入或条款后，旧决定不再适用。交互策略变更不能与重分析或单独报告刷新混用，正式交付答复则可同时包含报告及策略设置。具体命令见 [CLI 参考](CLI_REFERENCE_zh.md)。

### 显式续接额度

~~~toml
[continuation]
authorization_id = "continuation-1"
reason = "完成保存目标"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
~~~

该配置仅与 `--session-root` 一起使用。`remaining` 设置后续可用额度，历史费用和未知费用仍保留。`additional_attempts` 与 `additional_no_progress` 增加对应上限，不清零计数。

必须提供稳定的授权 ID 和原因。相同 ID 与条款只用于补完中断的授权，不会补回已经消耗的额度；改变条款需使用新 ID。不要直接修改账本。

单独授权不会重启已完成任务，也不会自动执行报告刷新。后续刷新所需的写作、装配和审计仍计入尝试额度，失败尝试另计。仅刷新报告不需要增加进程额度；若后续需要执行，应在创建任务时声明相应进程预算维度。

## 加载规则

`code-task init --config` 读取初始化设置，`execute --config` 读取执行、模型和预算设置；`research-session --code-task-config` 复用同一解析器。明确 CLI 参数优先于文件设置。

CodeTask 相对路径从命令工作目录解析。源码、任务、输出、解释器、benchmark 命令、基线文件和 `required_paths` 可用 `{config_dir}` 引用配置文件所在目录。不要使用资源环境变量插值；命令中含空格的路径须正确引用。`required_paths` 只检查路径是否存在。

注意：多数 CodeTask 分区仍会忽略未知键，拼写错误可能不生效；`workspace` 和 `ablation` 会拒绝额外字段，旧阶段选择也会报错。预算分区接受其支持的覆盖设置。

当前公开解析器不读取旧的 `implementation.max_repair_attempts`，请使用 `execute.repair_rounds`。旧外层流水线配置仅用于理解历史记录。

## CodeTask TOML 字段参考

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

Normal 默认最多修改四个允许文件，总编辑字符数为 12,000。Large 必须明确批准，且仍受绝对上限约束；单源码块上限为 32,000 字符。Provided 指标 JSON 可使用平数值映射，或 metrics、metric_values 包装。命令运行成功不等于目标改善。

#### 规划失败与修复

完整但格式不符合要求的已知响应，可以在限额内纠正或明确回退。提供方故障、预算耗尽、超时、未完成流和未核清操作不会被替换成离线成功。离线 greenfield 使用四文件开发骨架，不按关键词猜任务模块；未实现前报错，不生成测量值，文件上限小于四时拒绝该骨架。模型规划按实际任务选择结构，文件与行数上限不是填充配额。已保存计划保留原文件，不重建。

技术修复需要审阅，并沿用已接受的协议和进程限额；不授权方法变更、种子扩展或配对修订。复用修复结果先核对绑定输入和当前项目字节，保留快照及回滚能力。中断候选不会盲目重放。

#### 历史规划

方法迁移保留旧阶段、失败、摘要和账本；退役阶段的模型路由会被拒绝。已生成项目及导入的不可变已接受规划跳过重新规划。

<a id="workspace-mode-variants"></a>

`research-session --code-task-config` 保留配置的文件大小帽、稀疏 include/exclude 和源 venv 选择。声明的项目数据仍只支持 copy/worktree，稀疏模式不能绕过保护；源 venv 复用不能与托管任务环境准备同时使用。准备后的校验、测量及恢复沿用实际选中的解释器。候选修订复制已接受源码并保留该解释器；从 baseline 重做则重新应用原导入策略。若文件帽会使候选源码丢失，准备会停止，不会静默丢改动。

CodeTask 源码索引跳过符号链接及 Windows 重解析项。旧回执若曾绑定链接目标，需要明确检查，不会为凑出相同快照而重新读取目标。工作区隔离不等于操作系统沙箱。

## 工作区模式

### `auto`

这是已有项目的默认模式。可行时创建 detached Git worktree，否则使用受保护的复制，并记录原因。研究准备遇到未提交源码时使用 copy；仓库中的子目录项目使用 worktree 中对应的子目录。

### `copy`

复制符合规则的当前工作树文件，可以包含未提交源码。排除规则和大小限制可能造成遗漏，应检查准备记录。

### `git_worktree`

使用已提交的 HEAD 创建 detached worktree。源项目须在至少有一次提交的本地 Git 仓库中，不要求远程仓库。显式选择后若无法创建就失败，不降级为 copy，也不包含未提交源码。

### `sparse_copy`

独立 CodeTask 按 include/exclude 规则复制，同时排除 Git、虚拟环境、runs、缓存、构建、数据、模型和私密路径。此模式可能遗漏运行依赖。

研究准备支持 auto、copy、git_worktree 和 sparse_copy；声明的项目数据不能通过 sparse_copy 绕过复制保护。Greenfield TOML 默认 empty；采用 copy/sparse_copy 脚手架模式必须提供 code_root。CLI 的 `--workspace-mode` 不接受 empty。

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

## 在研究会话中使用 CodeTask

通过 `execution.code_task_config` 或 `--code-task-config` 引用同一文件。CodeTask 负责编辑和验证，研究应用负责测量和报告。复现 adapter 是明确允许同时声明独立正式命令的例外。

新建精确 CodeTask 请求的代码、可选环境准备、声明的基线/候选条件及报告/反馈共享原生任务，包括已支持的 copy/sparse 源码脚手架及必需新 Python 文件。环境只准备一次，条件测量与反馈复用；默认进程额度包含准备步骤，显式限制不改。任务 venv 不得静默替换明确选择的外部解释器。已声明测量的 auto/autonomous 任务还可调查材料并形成接受后的设计：实施直接消费原设计，批准绑定设计、接受意见和来源记录；证据变化会使旧实施批准失效。独立源码/环境准备可先于设计接受，接受设计不授予执行权限。CodeTask 基线复用、分阶段/checkpoint 新建及历史继续保留独特合同。工作区、编辑范围、独立验证和执行批准不丢；运行命令不等于批准执行。原生任务用 `simple-ar start --resume-task SESSION` 继续。原生工具需要 Chat 连接。Worktree 批准暂不能绑定 checkout 过滤、attributes、子模块或换行转换；需要这些语义时保留专家协议，或由用户明确选 copy。

要求 report 时，写作直接消费已接受的实施证据及真实修订关系，由原写作模块执行保存的整稿/分节、adaptive 规划、审阅及有界修订要求；必需标题顺序和引文上限仍是硬约束。完成交付同时要求已验证代码和当前规范稿，反馈后也不能拿旧稿充数。任务 max_output_tokens 在专业路由后保留，不修改命名连接；明确的逐次上限（包括 max_section_tokens）优先。缺少报告修订或仍有必需解决的意见时保持 partial，不当作完成。

新的原生专业报告只渲染明确图形计划，不把图数上限或章节标题当成猜图依据；已提供的作者图形仍可使用。历史装配保留原先保存的渲染策略。

明确的 greenfield 编辑范围和预算在完整候选发布前检查，涵盖补出的入口文件及 agent 接收成果。拒绝的候选不替换旧项目。原生反馈修改已接受的项目并保留其生成/API/运行产物合同，不重新生成架构，也不靠切换任务 kind 跳过整项目检查。纯技术精确任务不强造默认 score；科学结果合同不变。

数值编辑预算覆盖也适用于研究执行与恢复，但不能绕过保护路径或绝对上限。

## Execute 与 Budget

具体设置见[字段表](#execute-与-budget-字段)。连接重试、研究额度、进程限制、编辑大小和纠正轮数分别控制不同边界，不会互相清零用量。

<a id="edit-scope-behavior"></a>

## 编辑范围规则

文件修改只发生在 `code_task/workspace` 内。`allowed_patterns` 为空时，规范化且不受保护的路径可以编辑；非空时，文件必须匹配至少一个允许规则，同时不能匹配任何保护规则。

测试、benchmark、`.env`、秘密和凭据路径的内置保护始终保留。用户设置的 `protected_patterns` 追加保护，不替换默认规则。工作计划或当前批次的目标文件还会进一步缩小可编辑范围。

上下文选择、规划、编辑提案和修复均遵守范围。应用提案前再次检查工作区路径、范围、唯一精确的旧文本匹配和预算。空 `old` 只用于创建尚不存在且允许的文件，不能覆盖已有文件；新建文件沿用快照和失败回滚机制。
